#!/usr/bin/env python3
"""pdf_parser.py — Extract structured text from a PDF resume.

Fallback tool when system pdftotext is not available.
Requires: pypdf (pip install pypdf)

Usage:
    python3 pdf_parser.py <resume.pdf> [--out <output.txt>]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def extract_text(pdf_path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        print("ERROR: pypdf not installed. Run: pip install pypdf", file=sys.stderr)
        sys.exit(1)

    reader = PdfReader(str(pdf_path))
    pages = []
    for i, page in enumerate(reader.pages):
        text = page.extract_text()
        if text:
            pages.append(f"--- Page {i + 1} ---\n{text}")
    return "\n\n".join(pages)


def main():
    parser = argparse.ArgumentParser(description="Extract text from PDF resume")
    parser.add_argument("pdf", type=Path, help="Path to the PDF file")
    parser.add_argument("--out", type=Path, default=None, help="Output text file path")
    args = parser.parse_args()

    if not args.pdf.exists():
        print(f"ERROR: File not found: {args.pdf}", file=sys.stderr)
        sys.exit(1)

    text = extract_text(args.pdf)

    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"Extracted {len(text)} chars -> {args.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
