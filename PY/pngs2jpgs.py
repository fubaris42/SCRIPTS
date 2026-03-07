#!/usr/bin/env python3

from PIL import Image
import os

input_dir = "."
output_dir = os.path.join(input_dir, "converted_jpegs")
os.makedirs(output_dir, exist_ok=True)
for filename in os.listdir(input_dir):
    if filename.lower().endswith(".png"):
        img_path = os.path.join(input_dir, filename)
        img = Image.open(img_path).convert("RGB")
        jpeg_filename = os.path.splitext(filename)[0] + ".jpg"
        img.save(os.path.join(output_dir, jpeg_filename), "JPEG")
        print(f"Converted {filename} to {jpeg_filename}")
print("Conversion complete! Check the 'converted_jpegs' folder.")
