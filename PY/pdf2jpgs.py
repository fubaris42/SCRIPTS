#!/usr/bin/env python3

import fitz  # PyMuPDF
import os

base_dir = "."
output_base = "output_images"
os.makedirs(output_base, exist_ok=True)

for root, dirs, files in os.walk(base_dir):
    for file in files:
        if file.lower().endswith(".pdf"):
            pdf_path = os.path.join(root, file)
            relative_path = os.path.relpath(root, base_dir)
            pdf_name = os.path.splitext(file)[0]

            output_folder = os.path.join(output_base, relative_path, pdf_name)
            os.makedirs(output_folder, exist_ok=True)

            doc = fitz.open(pdf_path)
            for page_number in range(len(doc)):
                page = doc.load_page(page_number)
                pix = page.get_pixmap(dpi=300)
                output_path = os.path.join(output_folder, f"page_{page_number + 1}.jpg")
                pix.save(output_path)
            doc.close()

print("All PDFs have been processed.")
