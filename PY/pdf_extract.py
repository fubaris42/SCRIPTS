#!/usr/bin/env python3
from __future__ import annotations
import argparse
import logging
import multiprocessing
import os
import re
import sys
import time
import unicodedata
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Set, Tuple

try:
    import fitz
except ImportError:
    print(
        "PyMuPDF is not installed. Install with: pip install PyMuPDF", file=sys.stderr
    )
    sys.exit(1)

DEFAULT_OUTPUT_DIR = Path("extracted_pages")
DEFAULT_LOG_FILE = "pdf_extractor.log"
MAX_WORKERS_RATIO = 0.8
MAX_FILENAME_TEXT_LEN = 50
FILENAME_COLLISION_LIMIT = 10000


@dataclass(frozen=True)
class ExtractionConfig:
    pattern_inputs: List[str]
    use_regex: bool
    case_sensitive: bool
    whole_word: bool
    output_dir: str
    input_dirs: List[str]
    file_patterns: List[str]
    max_workers: int
    log_level: str
    recursive: bool
    dry_run: bool


@dataclass
class ProcessResult:
    pdf_path: str
    status: str
    pages_extracted: int = 0
    matches_found: int = 0
    total_pages: int = 0
    matched_pattern_indices: List[int] = field(default_factory=list)
    error: str | None = None
    processing_time: float = 0.0


@dataclass
class ExtractionStats:
    total_pdfs: int = 0
    processed_pdfs: int = 0
    failed_pdfs: int = 0
    total_pages_extracted: int = 0
    total_matches: int = 0
    processing_time: float = 0.0
    errors: List[str] = field(default_factory=list)


def setup_logger(output_dir: Path, log_level: str) -> logging.Logger:
    logger = logging.getLogger("PDFExtractor")
    logger.setLevel(logging.DEBUG)
    if logger.hasHandlers():
        logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    ch = logging.StreamHandler()
    ch.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(output_dir / DEFAULT_LOG_FILE, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except (PermissionError, OSError) as e:
        logger.warning(f"Cannot create log file: {e}. Logging to console only.")
    return logger


def sanitize_filename_component(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKD", text)
    t = "".join(ch for ch in t if ord(ch) >= 32)
    t = re.sub(r'[\\/*?:"<>|]', "_", t)
    t = re.sub(r"[\n\r\t]+", " ", t)
    t = t.strip()
    if len(t) > MAX_FILENAME_TEXT_LEN:
        t = t[:MAX_FILENAME_TEXT_LEN].rstrip()
    return t.strip(" _.")


def unique_output_path(base_dir: Path, base_name: str, ext: str = ".pdf") -> Path:
    candidate = base_dir / f"{base_name}{ext}"
    counter = 1
    while candidate.exists() and counter <= FILENAME_COLLISION_LIMIT:
        candidate = base_dir / f"{base_name}_({counter}){ext}"
        counter += 1
    if counter > FILENAME_COLLISION_LIMIT:
        raise RuntimeError("Too many filename collisions.")
    return candidate


def compile_patterns(
    pattern_inputs: List[str], use_regex: bool, case_sensitive: bool, whole_word: bool
) -> List[Tuple[int, re.Pattern, str]]:
    flags = 0 if case_sensitive else re.IGNORECASE
    compiled: List[Tuple[int, re.Pattern, str]] = []
    for idx, p in enumerate(pattern_inputs):
        p = p.strip()
        if not p:
            continue
        if use_regex:
            try:
                cp = re.compile(p, flags)
            except re.error as e:
                raise ValueError(f"Invalid regex pattern '{p}': {e}")
        else:
            esc = re.escape(p)
            if whole_word:
                esc = r"\b" + esc + r"\b"
            cp = re.compile(esc, flags)
        compiled.append((idx, cp, p))
    return compiled


def find_matches_in_doc_with_indices(
    doc: "fitz.Document", compiled_patterns: List[Tuple[int, re.Pattern, str]]
) -> Tuple[Dict[int, List[Tuple[int, str]]], Set[int]]:
    page_matches: Dict[int, List[Tuple[int, str]]] = {}
    matched_patterns: Set[int] = set()
    for page_idx, page in enumerate(doc):
        try:
            text = page.get_text("text")
        except Exception:
            text = page.get_text()
        found_on_page: List[Tuple[int, str]] = []
        for pat_idx, pat, _orig in compiled_patterns:
            for m in pat.finditer(text):
                found_on_page.append((pat_idx, m.group(0)))
                matched_patterns.add(pat_idx)
        if found_on_page:
            uniq = []
            seen = set()
            for pair in found_on_page:
                key = (pair[0], pair[1])
                if key not in seen:
                    uniq.append(pair)
                    seen.add(key)
            page_matches[page_idx] = uniq
    return page_matches, matched_patterns


def extract_pages_and_save(
    doc: "fitz.Document",
    pdf_path: Path,
    page_matches: Dict[int, List[Tuple[int, str]]],
    config: ExtractionConfig,
) -> int:
    written = 0
    out_root = Path(config.output_dir)
    input_dirs = [Path(d) for d in config.input_dirs]
    for page_idx, matches in page_matches.items():
        pdf_path_obj = pdf_path
        relative_parent = Path(".")
        for d in input_dirs:
            try:
                rel = pdf_path_obj.relative_to(d)
                relative_parent = rel.parent
                break
            except Exception:
                continue
        output_dir = out_root / relative_parent
        output_dir.mkdir(parents=True, exist_ok=True)
        first_match_text = matches[0][1] if matches else ""
        sanitized = sanitize_filename_component(first_match_text) or "match"
        base_name = f"{sanitized}_{pdf_path_obj.stem}_page_{page_idx+1}"
        out_path = unique_output_path(output_dir, base_name, ext=".pdf")
        new_doc = fitz.open()
        new_doc.insert_pdf(doc, from_page=page_idx, to_page=page_idx)
        new_doc.save(out_path)
        new_doc.close()
        written += 1
    return written


def process_pdf_worker_worker(config_dict: Dict, pdf_path_str: str) -> Dict:
    start = time.time()
    pdf_path = Path(pdf_path_str)
    try:
        config = ExtractionConfig(**config_dict)
    except Exception as e:
        return {
            "error": f"Invalid config in worker: {e}",
            "status": "failed",
            "pdf": pdf_path_str,
            "time": time.time() - start,
        }
    try:
        compiled = compile_patterns(
            config.pattern_inputs,
            config.use_regex,
            config.case_sensitive,
            config.whole_word,
        )
    except ValueError as e:
        return {
            "error": str(e),
            "status": "failed",
            "pdf": pdf_path_str,
            "time": time.time() - start,
        }
    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        return {
            "error": f"Could not open PDF: {e}",
            "status": "failed",
            "pdf": pdf_path_str,
            "time": time.time() - start,
        }
    total_pages = len(doc)
    if total_pages == 0:
        doc.close()
        return {
            "status": "failed",
            "error": "PDF has no pages",
            "pdf": pdf_path_str,
            "time": time.time() - start,
        }
    page_matches, matched_patterns = find_matches_in_doc_with_indices(doc, compiled)
    total_matches = sum(len(v) for v in page_matches.values())
    pages_written = 0
    if page_matches and not config.dry_run:
        try:
            pages_written = extract_pages_and_save(doc, pdf_path, page_matches, config)
        except Exception as e:
            doc.close()
            return {
                "status": "failed",
                "error": f"Error during extraction: {e}",
                "pdf": pdf_path_str,
                "time": time.time() - start,
            }
    doc.close()
    status = "success" if page_matches else "no_matches"
    return {
        "status": status,
        "pdf": pdf_path_str,
        "pages_extracted": pages_written if not config.dry_run else len(page_matches),
        "matches_found": total_matches,
        "total_pages": total_pages,
        "matched_pattern_indices": sorted(list(matched_patterns)),
        "time": time.time() - start,
    }


class PDFExtractor:
    def __init__(self, config: ExtractionConfig):
        self.config = config
        self.stats = ExtractionStats()
        self.out_dir = Path(config.output_dir)
        self.logger = setup_logger(self.out_dir, config.log_level)
        compile_patterns(
            config.pattern_inputs,
            config.use_regex,
            config.case_sensitive,
            config.whole_word,
        )

    def _find_pdf_files(self) -> List[Path]:
        pdfs = set()
        for d in self.config.input_dirs:
            dd = Path(d)
            if not dd.is_dir():
                self.logger.warning(f"Input directory not found: {dd}")
                continue
            for pat in self.config.file_patterns:
                searcher = dd.rglob if self.config.recursive else dd.glob
                for p in searcher(pat):
                    if p.is_file() and p.suffix.lower() == ".pdf":
                        pdfs.add(p.resolve())
        return sorted(pdfs)

    def run(self) -> ExtractionStats:
        start_time = time.time()
        self.logger.info("Starting PDF Page Extractor")
        self.logger.info(f"Output directory: {self.out_dir.resolve()}")
        if self.config.dry_run:
            self.logger.warning("DRY RUN enabled. No files will be written.")
        pdf_files = self._find_pdf_files()
        if not pdf_files:
            self.logger.warning("No PDF files found.")
            return self.stats
        self.stats.total_pdfs = len(pdf_files)
        if not self.config.dry_run:
            try:
                self.out_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                self.logger.error(f"Cannot create output directory: {e}")
                return self.stats
        config_dict = asdict(self.config)
        matched_pattern_global: Set[int] = set()
        max_workers = max(1, min(self.config.max_workers, (os.cpu_count() or 1)))
        self.logger.info(f"Using {max_workers} worker processes.")
        with ProcessPoolExecutor(max_workers=max_workers) as exe:
            futures = {
                exe.submit(process_pdf_worker_worker, config_dict, str(pdf)): pdf
                for pdf in pdf_files
            }
            completed = 0
            for fut in as_completed(futures):
                completed += 1
                pdf = futures[fut]
                try:
                    r = fut.result()
                except Exception as e:
                    self.logger.error(f"Worker exception for {pdf.name}: {e}")
                    self.stats.failed_pdfs += 1
                    self.stats.errors.append(f"{pdf.name}: {e}")
                    continue
                if r.get("status") == "failed":
                    self.stats.failed_pdfs += 1
                    self.stats.errors.append(
                        f"{Path(r.get('pdf')).name}: {r.get('error')}"
                    )
                    self.logger.error(
                        f"Failed: {Path(r.get('pdf')).name}: {r.get('error')}"
                    )
                else:
                    self.stats.processed_pdfs += 1
                    if r.get("status") == "success":
                        self.stats.total_pages_extracted += r.get("pages_extracted", 0)
                        self.stats.total_matches += r.get("matches_found", 0)
                        matched_pattern_global.update(
                            r.get("matched_pattern_indices", [])
                        )
                        self.logger.info(
                            f"Extracted {r.get('pages_extracted', 0)} pages from {Path(r.get('pdf')).name} ({r.get('matches_found', 0)} matches)"
                        )
                    elif r.get("status") == "no_matches":
                        self.logger.info(f"No matches in {Path(r.get('pdf')).name}")
        self.stats.processing_time = time.time() - start_time
        total_patterns = list(range(len(self.config.pattern_inputs)))
        unmatched = [i for i in total_patterns if i not in matched_pattern_global]
        if unmatched:
            self.logger.warning("Patterns with no matches:")
            for idx in unmatched:
                self.logger.warning(f"[{idx}] {self.config.pattern_inputs[idx]}")
        else:
            self.logger.info("All patterns matched at least once.")
        self.logger.info("Extraction complete")
        self.logger.info(f"Total time: {self.stats.processing_time:.2f}s")
        self.logger.info(
            f"PDFs processed: {self.stats.processed_pdfs}/{self.stats.total_pdfs}"
        )
        self.logger.info(f"Pages extracted: {self.stats.total_pages_extracted}")
        if self.stats.failed_pdfs:
            self.logger.warning(f"Failed PDFs: {self.stats.failed_pdfs}")
        return self.stats


def load_search_inputs(search_input: str) -> List[str]:
    p = Path(search_input)
    if p.exists() and p.is_file():
        txt = p.read_text(encoding="utf-8")
        lines = [line.strip() for line in txt.splitlines() if line.strip()]
        if not lines:
            raise ValueError(f"Pattern file '{p}' is empty.")
        return lines
    else:
        return [search_input]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract pages from PDFs using text search."
    )
    parser.add_argument("search_input", help="Search string or path to pattern file.")
    parser.add_argument("-o", "--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument(
        "-i", "--input-dirs", nargs="+", default=["."], help="Input directories."
    )
    parser.add_argument("-f", "--file-patterns", nargs="+", default=["*.pdf"])
    parser.add_argument("-r", "--regex", action="store_true", dest="use_regex")
    parser.add_argument("-c", "--case-sensitive", action="store_true")
    parser.add_argument(
        "-w",
        "--whole-word",
        action="store_true",
        help="Whole word match (ignored if regex).",
    )
    parser.add_argument("-R", "--recursive", action="store_true")
    parser.add_argument("-n", "--dry-run", action="store_true")
    parser.add_argument("-j", "--max-workers", type=int, help="Max worker processes.")
    parser.add_argument(
        "-l",
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
    )
    return parser.parse_args()


def main() -> int:
    try:
        args = parse_args()
        pattern_inputs = load_search_inputs(args.search_input)
        max_workers = args.max_workers or max(
            1, int((os.cpu_count() or 1) * MAX_WORKERS_RATIO)
        )
        config = ExtractionConfig(
            pattern_inputs=pattern_inputs,
            use_regex=args.use_regex,
            case_sensitive=args.case_sensitive,
            whole_word=args.whole_word,
            output_dir=args.output_dir,
            input_dirs=args.input_dirs,
            file_patterns=args.file_patterns,
            max_workers=max_workers,
            log_level=args.log_level,
            recursive=args.recursive,
            dry_run=args.dry_run,
        )
        if config.use_regex:
            compile_patterns(
                config.pattern_inputs,
                config.use_regex,
                config.case_sensitive,
                config.whole_word,
            )
        extractor = PDFExtractor(config)
        stats = extractor.run()
        print("Summary")
        print(f"Total Time: {stats.processing_time:.2f}s")
        print(f"PDFs Processed: {stats.processed_pdfs}/{stats.total_pdfs}")
        print(f"Pages Extracted: {stats.total_pages_extracted}")
        if stats.failed_pdfs:
            print(f"PDFs Failed: {stats.failed_pdfs}")
            return 1
        return 0
    except (ValueError, FileNotFoundError) as e:
        print(f"Configuration Error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Process interrupted by user.", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
