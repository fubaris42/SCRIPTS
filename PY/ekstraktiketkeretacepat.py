#!/usr/bin/env python3

import fitz  # PyMuPDF
from pathlib import Path

input_dir = Path(".")
output_dir = Path("./out")
output_dir.mkdir(parents=True, exist_ok=True)

for pdf_file in input_dir.glob("*.pdf"):
    print(f"Processing {pdf_file.name}...")
    doc = fitz.open(pdf_file)
    output = fitz.open()

    for i in range(0, len(doc), 3):
        output.insert_pdf(doc, from_page=i, to_page=i)

    output_path = output_dir / f"{pdf_file.stem}_every3rd.pdf"
    output.save(output_path)
    output.close()
    doc.close()

    print(f"Saved to {output_path.name}")

print("Done.")
