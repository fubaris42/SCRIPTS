import fitz  # PyMuPDF
import sys
import argparse
from pathlib import Path


def extract_images_from_pdf(pdf_path, output_dir):
    try:
        pdf_document = fitz.open(pdf_path)
    except Exception as e:
        print(f"❌ Failed to open {pdf_path}: {e}")
        return

    # Prepare a dedicated subfolder named after the PDF
    pdf_name = pdf_path.stem
    pdf_output_dir = output_dir / pdf_name

    images_found = False

    # Loop through every page
    for page_number in range(len(pdf_document)):
        page = pdf_document[page_number]
        image_list = page.get_images(full=True)

        if image_list:
            if not images_found:
                # Only create the folder if the PDF actually contains images
                pdf_output_dir.mkdir(parents=True, exist_ok=True)
                images_found = True

            for image_index, img in enumerate(image_list, start=1):
                xref = img[0]
                base_image = pdf_document.extract_image(xref)
                image_bytes = base_image["image"]
                image_ext = base_image["ext"]

                image_filename = (
                    pdf_output_dir / f"page{page_number + 1}_{image_index}.{image_ext}"
                )

                with open(image_filename, "wb") as image_file:
                    image_file.write(image_bytes)

    pdf_document.close()

    if images_found:
        print(f"✔ Extracted images to: {pdf_output_dir}")
    else:
        print(f"⚠ No images found in: {pdf_path.name}")


def main():
    # 1. Set up argument parsing (handles sys.argv automatically)
    parser = argparse.ArgumentParser(
        description="Recursively extract photos from all PDFs in a directory."
    )
    parser.add_argument(
        "input_path", help="The directory to scan recursively (or a specific PDF file)."
    )
    parser.add_argument(
        "-o",
        "--output",
        help="The base folder to save extracted images.",
        default="extracted_photos",
    )

    args = parser.parse_args()

    input_path = Path(args.input_path)
    output_base_dir = Path(args.output)

    # 2. Validate input
    if not input_path.exists():
        print(f"Error: The path '{input_path}' does not exist.")
        sys.exit(1)

    # 3. Process a single file
    if input_path.is_file() and input_path.suffix.lower() == ".pdf":
        print(f"Processing single file: {input_path}")
        extract_images_from_pdf(input_path, output_base_dir)

    # 4. Process a directory recursively
    elif input_path.is_dir():
        print(f"Scanning directory recursively: {input_path}")

        # .rglob("*.pdf") finds all PDFs in the folder AND all sub-folders
        pdf_files = list(input_path.rglob("*.pdf"))

        if not pdf_files:
            print(
                "No PDF files found in the specified directory or its subdirectories."
            )
            sys.exit(0)

        for pdf_file in pdf_files:
            extract_images_from_pdf(pdf_file, output_base_dir)
    else:
        print("Please provide a valid directory or .pdf file.")


if __name__ == "__main__":
    main()
