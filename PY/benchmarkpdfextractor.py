#!/usr/bin/env python3
"""PDFExtractor — speed benchmark harness.

Measures:
  - Pattern compile time
  - PDF discovery time
  - Per-file throughput (pages/sec, MB/sec)
  - Total wall-clock time
  - Worker efficiency (ideal vs actual speedup)
  - Memory high-water mark (RSS)

Usage:
    python benchmark.py -s /path/to/pdfs -p invoice -p "INV-\\d{6}" --regex
    python benchmark.py -s /path/to/pdfs -f keywords.txt --workers 4 --runs 3
"""

import argparse
import gc
import logging
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# ── optional memory tracking ──────────────────────────────────────────────────
try:
    import resource
    _HAS_RESOURCE = True
except ImportError:
    _HAS_RESOURCE = False  # Windows — use psutil fallback

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False

# ── constants ─────────────────────────────────────────────────────────────────

MB:             float = 1024 * 1024
LOG_FORMAT:     str   = "%(asctime)s.%(msecs)03d  [%(levelname)-8s]  %(name)s  %(message)s"
LOG_DATEFMT:    str   = "%Y-%m-%dT%H:%M:%S"
SEPARATOR:      str   = "─" * 72
SECTION:        str   = "═" * 72

# ── data classes ──────────────────────────────────────────────────────────────

@dataclass
class CorpusStats:
    """Metadata about the PDF corpus being benchmarked."""
    pdf_count:   int
    total_pages: int
    total_bytes: int

    @property
    def total_mb(self) -> float:
        return self.total_bytes / MB

    @property
    def avg_pages(self) -> float:
        return self.total_pages / self.pdf_count if self.pdf_count else 0.0

    @property
    def avg_mb(self) -> float:
        return self.total_mb / self.pdf_count if self.pdf_count else 0.0


@dataclass
class RunResult:
    """Timing and throughput data for a single benchmark run."""
    run_index:        int
    wall_seconds:     float
    pages_extracted:  int
    files_scanned:    int
    peak_rss_mb:      float = 0.0
    errors:           list[str] = field(default_factory=list)

    @property
    def pages_per_second(self) -> float:
        return self.pages_extracted / self.wall_seconds if self.wall_seconds else 0.0

    @property
    def files_per_second(self) -> float:
        return self.files_scanned / self.wall_seconds if self.wall_seconds else 0.0


@dataclass
class BenchmarkSummary:
    """Aggregated statistics across multiple runs."""
    corpus:       CorpusStats
    runs:         list[RunResult]
    n_workers:    int
    pattern_count: int

    @property
    def wall_times(self) -> list[float]:
        return [r.wall_seconds for r in self.runs]

    @property
    def mean_wall(self) -> float:
        return statistics.mean(self.wall_times)

    @property
    def stdev_wall(self) -> float:
        return statistics.stdev(self.wall_times) if len(self.wall_times) > 1 else 0.0

    @property
    def best_wall(self) -> float:
        return min(self.wall_times)

    @property
    def mean_pages_per_sec(self) -> float:
        return statistics.mean(r.pages_per_second for r in self.runs)

    @property
    def mean_mb_per_sec(self) -> float:
        return (self.corpus.total_mb / self.mean_wall) if self.mean_wall else 0.0

    @property
    def theoretical_speedup(self) -> float:
        """Amdahl's Law estimate assuming ~80% parallelisable work."""
        p = 0.80
        return 1.0 / ((1 - p) + (p / self.n_workers))

    @property
    def actual_speedup(self) -> float:
        """Actual speedup vs single-worker baseline (only valid if n_workers > 1)."""
        return 0.0  # requires separate single-worker run; see report


# ── corpus analyser ───────────────────────────────────────────────────────────

def analyse_corpus(src: str, recursive: bool) -> CorpusStats:
    """Scan the corpus directory and collect aggregate metadata.

    Args:
        src:       Path to the source folder.
        recursive: Whether to include sub-directories.

    Returns:
        A ``CorpusStats`` instance with file count, page count, and total size.
    """
    import fitz

    glob_expr = "**/*.pdf" if recursive else "*.pdf"
    pdf_paths = list(Path(src).glob(glob_expr))

    total_pages = 0
    total_bytes = 0

    log = logging.getLogger("benchmark.corpus")
    log.info("Analysing %d PDF file(s) in %r ...", len(pdf_paths), src)

    for pdf_path in pdf_paths:
        total_bytes += pdf_path.stat().st_size
        try:
            doc = fitz.open(str(pdf_path))
            total_pages += doc.page_count
            doc.close()
        except Exception:
            pass  # corrupt / encrypted — skip page count

    return CorpusStats(
        pdf_count   = len(pdf_paths),
        total_pages = total_pages,
        total_bytes = total_bytes,
    )


# ── memory helpers ────────────────────────────────────────────────────────────

def current_rss_mb() -> float:
    """Return the current process RSS in megabytes.

    Returns:
        RSS in MB, or 0.0 if no memory-tracking library is available.
    """
    if _HAS_PSUTIL:
        return psutil.Process().memory_info().rss / MB
    if _HAS_RESOURCE:
        # Linux: getrusage returns KB; macOS returns bytes
        ru = resource.getrusage(resource.RUSAGE_SELF)
        divisor = 1024 if sys.platform == "linux" else MB
        return ru.ru_maxrss / divisor
    return 0.0


# ── single run ────────────────────────────────────────────────────────────────

def run_once(
    cfg:       "ExtractionConfig",  # type: ignore[name-defined]
    run_index: int,
    corpus:    CorpusStats,
) -> RunResult:
    """Execute one full extraction pass and return timing + throughput data.

    Args:
        cfg:        Extraction configuration.
        run_index:  1-based run number (for labelling).
        corpus:     Pre-computed corpus metadata.

    Returns:
        A ``RunResult`` with wall time, throughput, and memory usage.
    """
    # Import here so benchmark.py can be imported without pdfextractor on path
    sys.path.insert(0, str(Path(__file__).parent))
    from pdfextractor import extract  # noqa: PLC0415

    errors_seen: list[str] = []

    def _capture_errors(msg: str) -> None:
        if "[ERROR" in msg:
            errors_seen.append(msg)

    gc.collect()
    rss_before = current_rss_mb()

    t_start = time.perf_counter()
    matched = extract(cfg, progress_cb=_capture_errors)
    t_end   = time.perf_counter()

    rss_after = current_rss_mb()

    return RunResult(
        run_index        = run_index,
        wall_seconds     = t_end - t_start,
        pages_extracted  = len(matched),
        files_scanned    = corpus.pdf_count,
        peak_rss_mb      = max(rss_before, rss_after),
        errors           = errors_seen,
    )


# ── report printer ────────────────────────────────────────────────────────────

def print_report(summary: BenchmarkSummary) -> None:
    """Print the full benchmark report to stdout.

    Args:
        summary: Aggregated benchmark results.
    """
    c  = summary.corpus
    sep = SEPARATOR
    sec = SECTION

    print(f"\n{sec}")
    print("  PDFExtractor — Benchmark Report")
    print(sec)

    print(f"\n{'CORPUS':}")
    print(sep)
    print(f"  Files          : {c.pdf_count:,}")
    print(f"  Total pages    : {c.total_pages:,}")
    print(f"  Total size     : {c.total_mb:,.1f} MB")
    print(f"  Avg pages/file : {c.avg_pages:.1f}")
    print(f"  Avg size/file  : {c.avg_mb:.2f} MB")

    print(f"\n{'CONFIGURATION':}")
    print(sep)
    print(f"  Workers        : {summary.n_workers}")
    print(f"  Patterns       : {summary.pattern_count}")
    print(f"  Runs           : {len(summary.runs)}")

    print(f"\n{'PER-RUN RESULTS':}")
    print(sep)
    print(f"  {'Run':<6} {'Wall (s)':<12} {'Pages/s':<12} {'Files/s':<12} {'RSS MB':<10} {'Errors'}")
    print(f"  {'-'*6} {'-'*12} {'-'*12} {'-'*12} {'-'*10} {'-'*6}")
    for r in summary.runs:
        print(
            f"  {r.run_index:<6} "
            f"{r.wall_seconds:<12.3f} "
            f"{r.pages_per_second:<12.1f} "
            f"{r.files_per_second:<12.1f} "
            f"{r.peak_rss_mb:<10.1f} "
            f"{len(r.errors)}"
        )

    print(f"\n{'AGGREGATE':}")
    print(sep)
    print(f"  Mean wall time : {summary.mean_wall:.3f}s  (±{summary.stdev_wall:.3f}s)")
    print(f"  Best wall time : {summary.best_wall:.3f}s")
    print(f"  Mean pages/s   : {summary.mean_pages_per_sec:.1f}")
    print(f"  Mean MB/s      : {summary.mean_mb_per_sec:.2f}")

    print(f"\n{'PARALLELISM ANALYSIS':}")
    print(sep)
    cpu_count = os.cpu_count() or 1
    print(f"  CPU cores      : {cpu_count}")
    print(f"  Workers used   : {summary.n_workers}")
    print(f"  Theoretical speedup (Amdahl p=0.80): {summary.theoretical_speedup:.2f}x")
    print(f"  Tip: run with --workers 1 and --workers {cpu_count} to measure actual speedup.")

    if any(r.errors for r in summary.runs):
        print(f"\n{'ERRORS DETECTED':}")
        print(sep)
        for r in summary.runs:
            for err in r.errors:
                print(f"  [Run {r.run_index}] {err}")

    print(f"\n{sec}\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Benchmark PDFExtractor speed.")
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("-p", "--patterns",      nargs="+", metavar="PAT")
    grp.add_argument("-f", "--patterns-file", metavar="FILE")
    p.add_argument("-s", "--src",     required=True)
    p.add_argument("-o", "--out",     default=str(Path(__file__).parent / "_bench_out"))
    p.add_argument("--regex",         action="store_true")
    p.add_argument("--case-sensitive", action="store_true")
    p.add_argument("--no-recursive",  action="store_true")
    p.add_argument("-w", "--workers", type=int, default=None)
    p.add_argument("-n", "--runs",    type=int, default=3, help="Number of timed runs.")
    return p


def main() -> None:
    """Entry point for the benchmark harness."""
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt=LOG_DATEFMT)
    log = logging.getLogger("benchmark")

    parser    = _build_parser()
    ns        = parser.parse_args()

    sys.path.insert(0, str(Path(__file__).parent))
    from pdfextractor import (  # noqa: PLC0415
        ExtractionConfig,
        load_patterns_file,
    )

    patterns: list[str] = (
        load_patterns_file(ns.patterns_file)
        if ns.patterns_file
        else (ns.patterns or [])
    )

    cfg = ExtractionConfig(
        patterns       = patterns,
        src            = ns.src,
        out            = ns.out,
        regex          = ns.regex,
        case_sensitive = ns.case_sensitive,
        recursive      = not ns.no_recursive,
        workers        = ns.workers,
    )

    n_workers = cfg.workers or os.cpu_count() or 4
    log.info("Analysing corpus ...")
    corpus = analyse_corpus(ns.src, cfg.recursive)

    if corpus.pdf_count == 0:
        log.error("No PDF files found in %r — aborting.", ns.src)
        sys.exit(1)

    log.info(
        "Corpus: %d files, %d pages, %.1f MB",
        corpus.pdf_count, corpus.total_pages, corpus.total_mb,
    )

    results: list[RunResult] = []
    for run_num in range(1, ns.runs + 1):
        log.info("--- Run %d / %d ---", run_num, ns.runs)
        result = run_once(cfg, run_num, corpus)
        results.append(result)
        log.info(
            "Run %d complete: %.3fs  |  %d pages extracted  |  %.1f pages/s",
            run_num, result.wall_seconds, result.pages_extracted, result.pages_per_second,
        )

    summary = BenchmarkSummary(
        corpus        = corpus,
        runs          = results,
        n_workers     = n_workers,
        pattern_count = len(patterns),
    )
    print_report(summary)


if __name__ == "__main__":
    main()
