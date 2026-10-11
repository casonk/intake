# REFS-PUBLIC.md - Public References

> Record external public repositories, datasets, documentation, APIs, or other
> public resources that this repository utilizes or depends on.
> This file is tracked and intentionally kept free of private or local-only details.

## Public Repositories

- No fixed external code repository is the main upstream; this repo processes local receipt PDFs and publishes local/private reports.

## Public Datasets and APIs

- No standing public datasets are consumed directly; input comes from local receipt files and local/private portfolio services.

## Documentation and Specifications

- https://tesseract-ocr.github.io/tessdoc/ - OCR engine installation and runtime reference
- https://pypi.org/project/pytesseract/ - Python Tesseract wrapper used by OCR extraction
- https://github.com/jsvine/pdfplumber - pdfplumber PDF text/table extraction reference used by receipt parsing
- https://pillow.readthedocs.io/ - Pillow image-processing reference used by OCR preparation
- https://pypdf.readthedocs.io/ - pypdf PDF handling reference used by receipt extraction
- https://jinja.palletsprojects.com/ - Jinja template reference used by Markdown/HTML report rendering
- https://toml.io/en/ - TOML configuration format reference used by local intake settings
- https://www.sqlite.org/docs.html - SQLite storage reference for the local receipt database
- https://platform.openai.com/docs - OpenAI API documentation for AI-assisted extraction flows when enabled

## Notes

- Local snowbridge and wiring-harness URLs are intentionally omitted here because they are portfolio-private surfaces rather than public references.
