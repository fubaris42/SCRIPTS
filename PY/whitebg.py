#!/usr/bin/env python3

import os
from PIL import Image
from rembg import remove, new_session

# Load the segmentation model once
session = new_session("u2net_human_seg")


def make_edges_sharp(image_with_alpha, threshold=200):
    """hard edges by thresholding alpha channel."""
    r, g, b, a = image_with_alpha.split()
    a = a.point(lambda p: 255 if p > threshold else 0)
    return Image.merge("RGBA", (r, g, b, a))


def process_image(input_path, output_path):
    try:
        with Image.open(input_path) as img:
            img = img.convert("RGBA")
            result = remove(img, session=session)

            # Sharpen edges
            result = make_edges_sharp(result, threshold=210)

            white_bg = Image.new("RGBA", result.size, (255, 255, 255, 255))
            white_bg.paste(result, (0, 0), result)
            white_bg = white_bg.convert("RGB")  # Final format: no transparency

            white_bg.save(output_path)
            print(f"Processed: {input_path} → {output_path}")
    except Exception as e:
        print(f"Failed to process {input_path}: {e}")


def process_images_recursive(input_dir, output_dir):
    supported_extensions = (".png", ".jpg", ".jpeg")
    os.makedirs(output_dir, exist_ok=True)

    for dirpath, dirnames, filenames in os.walk(input_dir):
        dirnames[:] = [
            d
            for d in dirnames
            if os.path.abspath(os.path.join(dirpath, d)) != os.path.abspath(output_dir)
        ]

        for filename in filenames:
            if filename.lower().endswith(supported_extensions):
                input_path = os.path.join(dirpath, filename)

                rel_path = os.path.relpath(dirpath, input_dir)
                save_dir = os.path.join(output_dir, rel_path)
                os.makedirs(save_dir, exist_ok=True)

                output_path = os.path.join(save_dir, filename)
                process_image(input_path, output_path)


input_directory = "."  # current working directory
output_directory = "white_background_output"

process_images_recursive(input_directory, output_directory)
