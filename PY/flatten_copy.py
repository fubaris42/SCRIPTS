#!/usr/bin/env python3

import sys
from pathlib import Path
import shutil

SOURCE_DIR = Path.cwd()
TARGET_DIR = SOURCE_DIR / "out"


file_counts = {}

try:
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
except Exception as e:
    print(f"Error creating target directory '{TARGET_DIR}': {e}", file=sys.stderr)
    sys.exit(1)

print(f"Source Directory: {SOURCE_DIR}")
print(f"Target Directory: {TARGET_DIR}")
print("-" * 30)


def flatten_files():
    """
    Recursively finds all files in SOURCE_DIR and copies them to TARGET_DIR,
    renaming duplicates.
    """
    copied_count = 0

    for source_path in SOURCE_DIR.rglob("*"):

        if source_path.is_dir():
            if source_path == TARGET_DIR:
                continue
            continue

        if source_path.is_relative_to(TARGET_DIR):
            continue

        original_name = source_path.name
        stem = source_path.stem
        suffix = source_path.suffix

        current_count = file_counts.get(original_name, 0)

        if current_count == 0:
            target_name = original_name
            file_counts[original_name] = 1
        else:
            target_name = f"{stem}-{current_count}{suffix}"
            file_counts[original_name] = current_count + 1

            print(
                f"  - Duplicate detected: '{original_name}' renamed to '{target_name}'"
            )

        target_path = TARGET_DIR / target_name

        try:
            shutil.copy2(source_path, target_path)

            print(
                f"  - Copied '{source_path.relative_to(SOURCE_DIR)}' to '{target_name}'"
            )
            copied_count += 1

        except Exception as e:
            print(f"Error copying {source_path}: {e}", file=sys.stderr)

    print("-" * 30)
    print(f"Done! Flattened {copied_count} file(s) into '{TARGET_DIR.name}'")


if __name__ == "__main__":
    flatten_files()
