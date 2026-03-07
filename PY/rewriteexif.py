#!/usr/bin/env python3

import os
import subprocess
from datetime import datetime
from PIL import Image, PngImagePlugin

image_extensions = (".jpg", ".jpeg", ".png", ".webp", ".heic")
video_extensions = (".webm",)

now = datetime.now()
date_str = now.strftime("%Y:%m:%d")
time_str = now.strftime("%H:%M:%S")
datetime_str = f"{date_str} {time_str}"


def update_jpeg_exif(file_path):
    try:
        subprocess.run(
            [
                "exiftool",
                f"-DateTimeOriginal={datetime_str}",
                f"-CreateDate={datetime_str}",
                f"-ModifyDate={datetime_str}",
                "-overwrite_original",
                file_path,
            ],
            check=True,
        )
        print(f"JPEG updated: {file_path}")
    except Exception as e:
        print(f"JPEG failed: {file_path} ({e})")


def update_png_metadata(file_path):
    try:
        img = Image.open(file_path)
        meta = PngImagePlugin.PngInfo()
        meta.add_text("Creation Time", datetime_str)
        img.save(file_path, "PNG", pnginfo=meta)
        print(f"PNG updated: {file_path}")
    except Exception as e:
        print(f"PNG failed: {file_path} ({e})")


def update_exiftool_generic(file_path):
    try:
        subprocess.run(
            [
                "exiftool",
                f"-DateTimeOriginal={datetime_str}",
                f"-CreateDate={datetime_str}",
                f"-ModifyDate={datetime_str}",
                "-overwrite_original",
                file_path,
            ],
            check=True,
        )
        print(f"Updated via exiftool: {file_path}")
    except Exception as e:
        print(f"Failed via exiftool: {file_path} ({e})")


def rewrite_all_metadata_in_cwd():
    cwd = os.getcwd()
    for root, _, files in os.walk(cwd):
        for file in files:
            path = os.path.join(root, file)
            ext = os.path.splitext(file.lower())[1]

            if ext in (".jpg", ".jpeg"):
                update_jpeg_exif(path)
            elif ext == ".png":
                update_png_metadata(path)
            elif ext in (".webp", ".webm", ".heic"):
                update_exiftool_generic(path)


rewrite_all_metadata_in_cwd()
