#!/usr/bin/env python3

import os
import fitz  # PyMuPDF
import concurrent.futures


def process_file(file_path):
    """
    Opens a supported file (PDF or image), converts it to a PDF in memory
    if necessary, and returns its byte representation. This function is
    designed to be run in a separate thread.
    Args:
        file_path (str): The file path to the PDF or image.

    Returns:
        bytes: The byte string of the PDF content, or None on error.
    """
    filename = os.path.basename(file_path)
    image_exts = (".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".gif")
    ext = os.path.splitext(filename)[1].lower()

    try:
        with fitz.open(file_path) as doc:
            if ext == ".pdf":
                output_bytes = doc.tobytes()
                print(f"Processed PDF: {filename}")
            elif ext in image_exts:
                output_bytes = doc.convert_to_pdf()
                print(f"Processed image: {filename}")
            else:
                return None
        return output_bytes
    except Exception as e:
        print(f"Error processing {filename}: {e}")
        return None


def main():
    """
    Finds all supported PDFs and images in the current directory, processes
    them in parallel, and merges them into a single output PDF file.
    """
    source_dir = "."
    output_file = "merged_all.pdf"

    supported_exts = (".pdf", ".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".gif")

    try:
        files_to_process = [
            os.path.join(source_dir, f)
            for f in os.listdir(source_dir)
            if f.lower().endswith(supported_exts)
        ]
        files_to_process.sort()

        if not files_to_process:
            print(f"No supported files {supported_exts} found in the directory.")
            return

    except OSError as e:
        print(f"Error accessing directory '{source_dir}': {e}")
        return

    print(f"Found {len(files_to_process)} files to process.")

    with concurrent.futures.ThreadPoolExecutor() as executor:
        results = executor.map(process_file, files_to_process)
        pdf_bytes_list = [res for res in results if res is not None]

    if not pdf_bytes_list:
        print("No files were successfully processed. Output file will not be created.")
        return

    print("\nAll files processed. Now merging...")

    final_doc = fitz.open()

    for pdf_bytes in pdf_bytes_list:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as source_doc:
            final_doc.insert_pdf(source_doc)

    try:
        final_doc.save(output_file, garbage=4, deflate=True, clean=True)
        print(f"\nSuccessfully merged {len(pdf_bytes_list)} files into '{output_file}'")
    except Exception as e:
        print(f"Error saving the final PDF: {e}")
    finally:
        final_doc.close()


if __name__ == "__main__":
    main()
