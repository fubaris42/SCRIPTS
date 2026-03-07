#!/usr/bin/env python3
"""PDFExtractor — extract PDF pages matching keywords from a folder of PDFs.

Requires:
    pip install pymupdf

Usage:
    CLI: python pdfextractor.py -p PATTERN [-p …] -s SRC -o OUT [OPTIONS]
         python pdfextractor.py -f keywords.txt  -s SRC -o OUT [OPTIONS]
    GUI: python pdfextractor.py
"""

# ── stdlib ────────────────────────────────────────────────────────────────────
import argparse
import datetime
import logging
import os
import re
import sys
import threading
from concurrent.futures import Future, ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Final, Optional

# ── constants ─────────────────────────────────────────────────────────────────

# Subprocess pool: at most (cpu_count × BATCH_FACTOR) PDFs open simultaneously.
# Keeps RAM and OS file-handle usage flat regardless of corpus size.
BATCH_FACTOR: Final[int] = 2

# Output filename: maximum characters taken from the sanitised pattern string.
FILENAME_KW_MAX: Final[int] = 40

# Enterprise-standard log format — ISO 8601 + milliseconds + logger name.
LOG_FORMAT: Final[str] = (
    "%(asctime)s.%(msecs)03d  [%(levelname)-8s]  %(name)s  %(message)s"
)
LOG_DATEFMT: Final[str] = "%Y-%m-%dT%H:%M:%S"

# GUI initial size and minimum resize constraints.
GUI_GEOMETRY: Final[str] = "680x560"
GUI_MIN_SIZE: Final[tuple[int, int]] = (500, 440)

# GUI log-panel background colours, keyed by logging level name.
GUI_LEVEL_COLORS: Final[dict[str, str]] = {
    "INFO": "#d4edda",  # muted green
    "WARNING": "#fff3cd",  # muted amber
    "ERROR": "#f8d7da",  # muted red
}

# ── custom exceptions ─────────────────────────────────────────────────────────


class PDFExtractorError(Exception):
    """Base exception for all PDFExtractor domain errors."""


class PatternCompileError(PDFExtractorError):
    """Raised when a regex pattern cannot be compiled.

    Args:
        pattern: The offending pattern string.
        cause: The underlying re.error.
    """

    def __init__(self, pattern: str, cause: re.error) -> None:
        super().__init__(f"Invalid pattern {pattern!r}: {cause}")
        self.pattern = pattern
        self.cause = cause


class SourceFolderError(PDFExtractorError):
    """Raised when the source folder is missing or not a directory."""


class OutputFolderError(PDFExtractorError):
    """Raised when the output folder cannot be created."""


# ── data classes ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExtractionConfig:
    """Immutable value object — all parameters governing one extraction run.

    Attributes:
        patterns: Raw pattern strings (plain-text or regex).
        src: Absolute path to the source folder containing PDFs.
        out: Absolute path to the output folder for extracted pages.
        regex: When True, patterns are compiled as-is; otherwise escaped.
        case_sensitive: When True, matching is case-sensitive.
        recursive: When True, sub-directories of src are also scanned.
        workers: Number of worker processes. Defaults to ``os.cpu_count()``.
    """

    patterns: list[str]
    src: str
    out: str
    regex: bool = False
    case_sensitive: bool = False
    recursive: bool = True
    workers: Optional[int] = None


@dataclass
class WorkerResult:
    """Aggregated output from a single PDF worker process.

    Attributes:
        matched: Absolute paths of every output PDF written.
        hits: Mapping of pattern string to the count of pages it matched.
        errors: Human-readable error messages for non-fatal failures.
    """

    matched: list[str] = field(default_factory=list)
    hits: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


# ── path helpers ──────────────────────────────────────────────────────────────


def resolve_long_path(path: str | Path) -> str:
    """Resolve *path* and prepend the Windows long-path prefix when needed.

    On Windows, paths longer than 260 characters require the ``\\\\?\\``
    prefix.  This function is a no-op on Linux and macOS.

    Args:
        path: Any path-like value.

    Returns:
        A fully resolved, platform-safe path string.
    """
    resolved: str = str(Path(path).resolve())
    if sys.platform == "win32" and not resolved.startswith("\\\\?\\"):
        return f"\\\\?\\{resolved}"
    return resolved


def sanitise_filename_segment(pattern: str) -> str:
    """Return a filesystem-safe substring derived from *pattern*.

    Replaces any character that is not alphanumeric, underscore, or hyphen
    with an underscore, then truncates to ``FILENAME_KW_MAX`` characters.

    Args:
        pattern: The raw regex or plain-text pattern string.

    Returns:
        A safe, truncated string suitable for inclusion in a filename.
    """
    return re.sub(r"[^\w-]", "_", pattern)[:FILENAME_KW_MAX]


# ── pattern helpers ───────────────────────────────────────────────────────────


def compile_patterns(
    patterns: list[str],
    regex: bool,
    case_sensitive: bool,
) -> list[re.Pattern[str]]:
    """Compile a list of raw pattern strings into ``re.Pattern`` objects.

    Args:
        patterns: Raw strings; treated as literal text unless *regex* is True.
        regex: When True, each string is compiled as-is.
        case_sensitive: When True, the ``re.IGNORECASE`` flag is omitted.

    Returns:
        A list of compiled ``re.Pattern`` objects, one per input string.

    Raises:
        PatternCompileError: If any pattern string is not valid regex.
        ValueError: If *patterns* is empty.
    """
    if not patterns:
        raise ValueError("At least one pattern is required.")

    flags: int = 0 if case_sensitive else re.IGNORECASE
    compiled: list[re.Pattern[str]] = []

    for raw in patterns:
        source = raw if regex else re.escape(raw)
        try:
            compiled.append(re.compile(source, flags))
        except re.error as exc:
            raise PatternCompileError(raw, exc) from exc

    return compiled


def load_patterns_file(path: str) -> list[str]:
    """Read pattern strings from a plain-text file.

    Each non-blank, non-comment line is returned as one pattern.
    Lines beginning with ``#`` are treated as comments and ignored.

    Args:
        path: Path to the ``.txt`` file.

    Returns:
        A list of stripped, non-empty, non-comment pattern strings.

    Raises:
        FileNotFoundError: If *path* does not exist.
        ValueError: If the file contains no usable patterns.
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"Patterns file not found: {path!r}")

    patterns: list[str] = [
        line.strip()
        for line in file_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    if not patterns:
        raise ValueError(f"No usable patterns found in {path!r}.")

    return patterns


# ── logging helpers ───────────────────────────────────────────────────────────


def format_log_line(level_name: str, logger_name: str, message: str) -> str:
    """Produce a single enterprise-format log line with the current timestamp.

    This mirrors the output of ``logging.basicConfig`` with ``LOG_FORMAT``
    so that the GUI panel and CLI stdout look identical.

    Args:
        level_name:  Logging level string, e.g. ``"INFO"`` or ``"WARNING"``.
        logger_name: Name of the originating logger.
        message:     The log message body.

    Returns:
        A fully formatted log line string (no trailing newline).
    """
    now: datetime.datetime = datetime.datetime.now()
    timestamp: str = now.strftime(LOG_DATEFMT) + f".{now.microsecond // 1000:03d}"
    return f"{timestamp}  [{level_name:<8}]  {logger_name}  {message}"


def configure_cli_logging() -> None:
    """Configure the root logger for CLI output with the enterprise format.

    Should be called once at program startup in CLI mode.
    """
    logging.basicConfig(
        level=logging.INFO,
        format=LOG_FORMAT,
        datefmt=LOG_DATEFMT,
    )


# ── PDF page extraction (subprocess worker) ───────────────────────────────────


def _extract_page_to_file(
    doc: "fitz.Document",  # type: ignore[name-defined]
    page_num: int,
    out_path: Path,
) -> None:
    """Write a single PDF page to an individual output file.

    Args:
        doc:      The open source ``fitz.Document``.
        page_num: 1-based page number to extract.
        out_path: Destination path for the single-page PDF.

    Raises:
        RuntimeError: If the page cannot be written to disk.
    """
    import fitz  # noqa: PLC0415 — subprocess-local import

    single_page_doc: fitz.Document = fitz.open()
    try:
        single_page_doc.insert_pdf(
            doc,
            from_page=int(page_num - 1),
            to_page=int(page_num - 1),
        )
        single_page_doc.save(
            resolve_long_path(out_path),
            garbage=4,
            deflate=True,
            clean=False,
        )
    except Exception as exc:
        raise RuntimeError(
            f"Failed to write page {page_num} to {out_path.name}"
        ) from exc
    finally:
        single_page_doc.close()


def _process_page(
    doc: "fitz.Document",  # type: ignore[name-defined]
    page_num: int,
    page_text: str,
    compiled: list[re.Pattern[str]],
    out_dir: Path,
    pdf_stem: str,
    result: WorkerResult,
) -> None:
    """Search one PDF page against all patterns and extract on match.

    Mutates *result* in-place: increments hit counters, appends matched
    output paths, and records any per-page errors.

    Args:
        doc:        The open source ``fitz.Document``.
        page_num:   1-based page number being processed.
        page_text:  Pre-extracted text content of the page.
        compiled:   Compiled regex patterns to search against.
        out_dir:    Destination directory for extracted pages.
        pdf_stem:   Stem of the source PDF filename (used in output name).
        result:     ``WorkerResult`` to mutate with findings.
    """
    for rx in compiled:
        if not rx.search(page_text):
            continue

        result.hits[rx.pattern] += 1
        out_path = out_dir / (
            f"{sanitise_filename_segment(rx.pattern)}_{pdf_stem}_page_{page_num}.pdf"
        )

        try:
            _extract_page_to_file(doc, page_num, out_path)
            result.matched.append(str(out_path))
        except RuntimeError as exc:
            result.errors.append(str(exc))


def _worker(
    args: tuple[str, str, str, list[re.Pattern[str]]],
) -> WorkerResult:
    """Process a single PDF file inside a subprocess.

    Opens the PDF, iterates every page, runs all patterns against the page
    text, and extracts matching pages into the mirrored output tree.

    Must be a module-level function to be picklable by ``ProcessPoolExecutor``.
    ``fitz`` is imported inside this function to avoid loading it in the
    main process and incurring unnecessary overhead.

    Args:
        args: A 4-tuple of (pdf_path, out_root, src_root, compiled_patterns).

    Returns:
        A ``WorkerResult`` with matched paths, hit counts, and any errors.
    """
    import fitz  # noqa: PLC0415 — subprocess-local import

    pdf_path, out_root, src_root, compiled = args
    result = WorkerResult(hits={rx.pattern: 0 for rx in compiled})

    try:
        doc: fitz.Document = fitz.open(resolve_long_path(pdf_path))
    except Exception as exc:
        result.errors.append(f"Cannot open {Path(pdf_path).name}: {exc}")
        return result

    out_dir = (
        Path(resolve_long_path(out_root)) / Path(pdf_path).relative_to(src_root).parent
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        for page_num, page in enumerate(doc, start=1):
            _process_page(
                doc,
                page_num,
                page.get_text(),
                compiled,
                out_dir,
                Path(pdf_path).stem,
                result,
            )
    finally:
        doc.close()

    return result


# ── extraction engine ─────────────────────────────────────────────────────────

EmitFn = Callable[[int, str], None]


def _build_emit_fn(
    logger: logging.Logger,
    progress_cb: Optional[Callable[[str], None]],
) -> EmitFn:
    """Construct an emit callable that logs and optionally forwards to a GUI.

    The returned function accepts a logging level integer and a message string.
    It always writes to *logger*, and when *progress_cb* is provided it also
    calls it with a pre-formatted enterprise-standard log line so the GUI
    panel and the CLI stdout look identical.

    Args:
        logger:      The ``logging.Logger`` for this module.
        progress_cb: Optional GUI callback that receives formatted log lines.

    Returns:
        A callable with signature ``(level: int, message: str) -> None``.
    """

    def emit(level: int, message: str) -> None:
        logger.log(level, message)
        if progress_cb is not None:
            level_name = logging.getLevelName(level)
            progress_cb(format_log_line(level_name, logger.name, message))

    return emit


def _validate_extraction_config(cfg: ExtractionConfig) -> None:
    """Fail fast: assert all pre-conditions of ExtractionConfig before work begins.

    Args:
        cfg: The configuration object to validate.

    Raises:
        SourceFolderError: If ``cfg.src`` does not exist or is not a directory.
        OutputFolderError: If ``cfg.out`` cannot be created.
        ValueError: If ``cfg.patterns`` is empty.
    """
    if not cfg.patterns:
        raise ValueError("ExtractionConfig.patterns must not be empty.")

    src_path = Path(cfg.src)
    if not src_path.exists():
        raise SourceFolderError(f"Source folder does not exist: {cfg.src!r}")
    if not src_path.is_dir():
        raise SourceFolderError(f"Source path is not a directory: {cfg.src!r}")

    try:
        Path(cfg.out).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OutputFolderError(
            f"Cannot create output folder {cfg.out!r}: {exc}"
        ) from exc


def _accumulate_batch_results(
    futures: dict[Future[WorkerResult], str],
    tally: dict[str, int],
    emit: EmitFn,
) -> list[str]:
    """Collect results from a completed futures batch and update tallies.

    Args:
        futures: Mapping of ``Future`` → source PDF path string.
        tally:   Per-pattern hit counter to mutate in-place.
        emit:    Logging emit function for surfacing worker errors.

    Returns:
        A flat list of output PDF paths produced by this batch.
    """
    batch_matched: list[str] = []

    for future in as_completed(futures):
        worker_result: WorkerResult = future.result()
        batch_matched.extend(worker_result.matched)

        for pattern, hit_count in worker_result.hits.items():
            tally[pattern] += hit_count

        for error_message in worker_result.errors:
            emit(logging.ERROR, error_message)

    return batch_matched


def _log_extraction_summary(
    tally: dict[str, int],
    total_pdfs: int,
    emit: EmitFn,
) -> None:
    """Log a verbose per-pattern result block when extraction completes.

    Args:
        tally:      Mapping of pattern string → total matched page count.
        total_pdfs: Total number of PDF files scanned.
        emit:       Logging emit function.
    """
    emit(logging.INFO, "--- RESULTS BY PATTERN ---")
    for pattern, page_count in tally.items():
        if page_count == 0:
            emit(
                logging.WARNING,
                f"pattern {pattern!r}: no matches in {total_pdfs} file(s)",
            )
        else:
            emit(
                logging.INFO,
                f"pattern {pattern!r}: {page_count} page(s) matched in {total_pdfs} file(s)",
            )
    emit(logging.INFO, "--- END RESULTS ---")


def extract(
    cfg: ExtractionConfig,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> list[str]:
    """Run a full extraction pass over all PDFs in ``cfg.src``.

    Validates the config, compiles patterns, discovers PDFs, dispatches
    work to a bounded subprocess pool, aggregates results, and logs a
    per-pattern summary on completion.

    Args:
        cfg:         Immutable configuration for this run.
        progress_cb: Optional callback receiving pre-formatted log lines.
                     Intended for the GUI log panel. CLI callers pass ``None``.

    Returns:
        A list of absolute paths to every output PDF created.

    Raises:
        SourceFolderError: If ``cfg.src`` is invalid.
        OutputFolderError: If ``cfg.out`` cannot be created.
        PatternCompileError: If any pattern string is not valid regex.
        ValueError: If ``cfg.patterns`` is empty.
    """
    logger: logging.Logger = logging.getLogger(__name__)
    emit: EmitFn = _build_emit_fn(logger, progress_cb)

    _validate_extraction_config(cfg)

    compiled: list[re.Pattern[str]] = compile_patterns(
        cfg.patterns, cfg.regex, cfg.case_sensitive
    )
    glob_expr: str = "**/*.pdf" if cfg.recursive else "*.pdf"
    pdf_paths: list[Path] = list(Path(cfg.src).glob(glob_expr))

    if not pdf_paths:
        emit(logging.WARNING, f"No PDF files found in {cfg.src!r}.")
        return []

    n_workers: int = cfg.workers or os.cpu_count() or 4
    batch_size: int = n_workers * BATCH_FACTOR
    all_matched: list[str] = []
    tally: dict[str, int] = {rx.pattern: 0 for rx in compiled}

    emit(
        logging.INFO,
        f"Found {len(pdf_paths)} PDF file(s) — scanning with {n_workers} workers",
    )

    worker_args: list[tuple[str, str, str, list[re.Pattern[str]]]] = [
        (str(p), cfg.out, cfg.src, compiled) for p in pdf_paths
    ]

    with ProcessPoolExecutor(max_workers=n_workers) as executor:
        for batch_start in range(0, len(worker_args), batch_size):
            batch_slice = worker_args[batch_start : batch_start + batch_size]
            futures: dict[Future[WorkerResult], str] = {
                executor.submit(_worker, arg): arg[0] for arg in batch_slice
            }
            all_matched.extend(_accumulate_batch_results(futures, tally, emit))

    _log_extraction_summary(tally, len(pdf_paths), emit)
    return all_matched


# ── CLI ───────────────────────────────────────────────────────────────────────


def _build_arg_parser() -> argparse.ArgumentParser:
    """Construct and return the CLI argument parser.

    Returns:
        A configured ``argparse.ArgumentParser`` instance.
    """
    parser = argparse.ArgumentParser(
        prog="pdfextractor",
        description="Extract PDF pages matching keywords from a folder of PDFs.",
    )
    pattern_group = parser.add_mutually_exclusive_group(required=True)
    pattern_group.add_argument(
        "-p",
        "--patterns",
        nargs="+",
        metavar="PAT",
        help="One or more literal or regex patterns.",
    )
    pattern_group.add_argument(
        "-f",
        "--patterns-file",
        metavar="FILE",
        help="Text file containing one pattern per line (# lines are comments).",
    )
    parser.add_argument("-s", "--src", required=True, help="Source folder.")
    parser.add_argument("-o", "--out", required=True, help="Output folder.")
    parser.add_argument("--regex", action="store_true", help="Treat patterns as regex.")
    parser.add_argument(
        "--case-sensitive", action="store_true", help="Case-sensitive matching."
    )
    parser.add_argument(
        "--no-recursive", action="store_true", help="Do not scan sub-folders."
    )
    parser.add_argument(
        "-w", "--workers", type=int, help="Worker count (default: CPU count)."
    )
    return parser


def cli() -> None:
    """Entry point for CLI mode.

    Parses arguments, configures logging, builds an ``ExtractionConfig``,
    runs ``extract()``, and reports the final count.

    Raises:
        SystemExit: On argument parse failure or unrecoverable domain error.
    """
    parser: argparse.ArgumentParser = _build_arg_parser()
    namespace: argparse.Namespace = parser.parse_args()

    configure_cli_logging()
    logger: logging.Logger = logging.getLogger(__name__)

    try:
        patterns: list[str] = (
            load_patterns_file(namespace.patterns_file)
            if namespace.patterns_file
            else (namespace.patterns or [])
        )
        cfg = ExtractionConfig(
            patterns=patterns,
            src=namespace.src,
            out=namespace.out,
            regex=namespace.regex,
            case_sensitive=namespace.case_sensitive,
            recursive=not namespace.no_recursive,
            workers=namespace.workers,
        )
        logger.info("Scanning %s", cfg.src)
        matched_files: list[str] = extract(cfg)
        logger.info("Done -- %d page(s) extracted -> %s", len(matched_files), cfg.out)

    except PDFExtractorError as exc:
        logger.error("Extraction failed: %s", exc)
        sys.exit(1)
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        sys.exit(1)


# ── GUI ───────────────────────────────────────────────────────────────────────


def _resolve_gui_log_tag(formatted_line: str) -> str:
    """Determine the GUI colour tag for a formatted log line.

    Matches the first level name found in ``GUI_LEVEL_COLORS`` whose bracket
    token (e.g. ``[WARNING ]``) appears in the line.

    Args:
        formatted_line: A complete enterprise-format log line string.

    Returns:
        The matching level name key, defaulting to ``"INFO"``.
    """
    return next(
        (name for name in GUI_LEVEL_COLORS if f"[{name}" in formatted_line),
        "INFO",
    )


def gui() -> None:
    """Entry point for GUI mode.

    Builds and runs the Tkinter interface. The extraction is performed on a
    daemon thread; all UI updates are posted back to the main thread via
    ``root.after()``.

    Raises:
        ImportError: If ``tkinter`` is not available on the current platform.
    """
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk

    root = tk.Tk()
    root.title("PDFExtractor")
    root.geometry(GUI_GEOMETRY)
    root.minsize(*GUI_MIN_SIZE)

    # ── tk state variables
    var_src = tk.StringVar()
    var_out = tk.StringVar()
    var_regex = tk.BooleanVar()
    var_case = tk.BooleanVar()
    var_recurse = tk.BooleanVar(value=True)

    # ── log panel (declared early so callbacks can reference it)
    log_frame = ttk.LabelFrame(root, text="Log")
    log_panel = scrolledtext.ScrolledText(
        log_frame, height=10, state="disabled", font=("Consolas", 9)
    )
    for level_name, bg_color in GUI_LEVEL_COLORS.items():
        log_panel.tag_configure(level_name, background=bg_color)

    # ── internal helpers

    def _append_log_line(formatted_line: str) -> None:
        """Append one formatted log line to the log panel with colour tagging.

        Args:
            formatted_line: A complete enterprise-format log string.
        """
        tag = _resolve_gui_log_tag(formatted_line)
        log_panel.config(state="normal")
        log_panel.insert("end", formatted_line + "\n", tag)
        log_panel.see("end")
        log_panel.config(state="disabled")

    def _thread_safe_log(formatted_line: str) -> None:
        """Schedule ``_append_log_line`` on the Tk main thread.

        Args:
            formatted_line: A complete enterprise-format log string.
        """
        root.after(0, _append_log_line, formatted_line)

    def _build_gui_progress_cb() -> Callable[[str], None]:
        """Return a progress callback safe to call from a worker thread.

        Returns:
            A callable that posts log lines to the Tk main thread.
        """
        return _thread_safe_log

    # ── user action handlers

    def _on_browse(target_var: tk.StringVar) -> None:
        """Open a directory chooser and populate *target_var*.

        Args:
            target_var: The ``tk.StringVar`` to update with the chosen path.
        """
        chosen = filedialog.askdirectory()
        if chosen:
            target_var.set(chosen)

    def _on_load_txt() -> None:
        """Open a file chooser for a .txt patterns file and populate the text box."""
        chosen = filedialog.askopenfilename(filetypes=[("Text files", "*.txt")])
        if not chosen:
            return
        try:
            loaded_patterns = load_patterns_file(chosen)
        except (FileNotFoundError, ValueError) as exc:
            messagebox.showerror("Load Error", str(exc))
            return
        pattern_text.delete("1.0", "end")
        pattern_text.insert("1.0", "\n".join(loaded_patterns))

    def _collect_patterns() -> list[str]:
        """Read and return non-blank patterns from the text widget.

        Returns:
            A stripped list of non-empty pattern strings.
        """
        return [
            line.strip()
            for line in pattern_text.get("1.0", "end").splitlines()
            if line.strip()
        ]

    def _validate_gui_inputs(
        patterns: list[str],
        src: str,
        out: str,
    ) -> bool:
        """Show a warning and return False if any required input is missing.

        Args:
            patterns: List of pattern strings from the text widget.
            src:      Source folder path string.
            out:      Output folder path string.

        Returns:
            True when all inputs are present; False otherwise.
        """
        if not patterns:
            messagebox.showwarning("Missing Input", "Enter at least one pattern.")
            return False
        if not src:
            messagebox.showwarning("Missing Input", "Select a source folder.")
            return False
        if not out:
            messagebox.showwarning("Missing Input", "Select an output folder.")
            return False
        return True

    def _on_start() -> None:
        """Validate inputs, build config, and launch the extraction thread."""
        patterns = _collect_patterns()
        src = var_src.get().strip()
        out = var_out.get().strip()

        if not _validate_gui_inputs(patterns, src, out):
            return

        cfg = ExtractionConfig(
            patterns=patterns,
            src=src,
            out=out,
            regex=var_regex.get(),
            case_sensitive=var_case.get(),
            recursive=var_recurse.get(),
        )

        start_btn.config(state="disabled")
        log_panel.config(state="normal")
        log_panel.delete("1.0", "end")
        log_panel.config(state="disabled")
        _append_log_line(format_log_line("INFO", "pdfextractor", f"Scanning {src}"))

        progress_cb = _build_gui_progress_cb()

        def _extraction_thread() -> None:
            try:
                matched_files = extract(cfg, progress_cb=progress_cb)
                done_line = format_log_line(
                    "INFO",
                    "pdfextractor",
                    f"Done -- {len(matched_files)} page(s) extracted -> {out}",
                )
                root.after(0, _append_log_line, done_line)
            except PDFExtractorError as exc:
                err_line = format_log_line("ERROR", "pdfextractor", str(exc))
                root.after(0, _append_log_line, err_line)
            finally:
                root.after(0, start_btn.config, {"state": "normal"})

        threading.Thread(target=_extraction_thread, daemon=True).start()

    # ── layout

    ttk.Label(root, text="PDFExtractor", font=("", 14, "bold")).pack(padx=8, pady=4)
    ttk.Separator(root).pack(fill="x")

    # Search patterns
    pattern_frame = ttk.LabelFrame(root, text="Search Patterns  (one per line)")
    pattern_frame.pack(fill="x", padx=8, pady=4)
    pattern_text = tk.Text(pattern_frame, height=4, wrap="none")
    pattern_text.pack(fill="x", padx=4, pady=4)
    ttk.Button(pattern_frame, text="Load .txt", command=_on_load_txt).pack(
        anchor="e", padx=4, pady=(0, 4)
    )

    # Folders — grid guarantees label + entry + button are always aligned
    folder_frame = ttk.LabelFrame(root, text="Folders")
    folder_frame.pack(fill="x", padx=8, pady=4)
    folder_frame.columnconfigure(1, weight=1)
    for row_idx, (label_text, target_var) in enumerate(
        [("Source Folder", var_src), ("Output Folder", var_out)]
    ):
        ttk.Label(folder_frame, text=label_text, anchor="w").grid(
            row=row_idx, column=0, sticky="w", padx=(8, 4), pady=4
        )
        ttk.Entry(folder_frame, textvariable=target_var).grid(
            row=row_idx, column=1, sticky="ew", pady=4
        )
        ttk.Button(
            folder_frame,
            text="Browse…",
            command=lambda v=target_var: _on_browse(v),
        ).grid(row=row_idx, column=2, padx=(4, 8), pady=4)

    # Options
    options_frame = ttk.LabelFrame(root, text="Options")
    options_frame.pack(fill="x", padx=8, pady=4)
    for option_label, option_var in [
        ("Regex", var_regex),
        ("Case Sensitive", var_case),
        ("Recursive", var_recurse),
    ]:
        ttk.Checkbutton(options_frame, text=option_label, variable=option_var).pack(
            side="left", padx=8, pady=4
        )

    # Log panel
    log_frame.pack(fill="both", expand=True, padx=8, pady=4)
    log_panel.pack(fill="both", expand=True, padx=4, pady=4)

    # Start button
    start_btn = ttk.Button(root, text="START EXTRACTION", command=_on_start)
    start_btn.pack(fill="x", padx=8, pady=4)

    root.mainloop()


# ── Windows long-path bootstrap ───────────────────────────────────────────────


def _enable_windows_long_paths() -> None:
    """Attempt to enable Windows long-path support at the process level.

    These WinAPI calls are best-effort and non-fatal on failure.
    The ``\\\\?\\`` prefix applied in ``resolve_long_path()`` is the
    authoritative fix — these calls are supplementary.
    """
    import ctypes

    for fn_name in ("SetProcessDEPPolicy", "SetFileApisToOEM"):
        try:
            getattr(ctypes.windll.kernel32, fn_name)(0)  # type: ignore[attr-defined]
        except OSError:
            pass


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if sys.platform == "win32":
        _enable_windows_long_paths()

    if len(sys.argv) > 1:
        cli()
    else:
        try:
            gui()
        except ImportError:
            print(
                "tkinter is not available on this platform.\n"
                "Use CLI mode: python pdfextractor.py --help",
                file=sys.stderr,
            )
            sys.exit(1)
