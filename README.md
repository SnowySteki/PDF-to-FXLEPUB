# PDF to FXLEPUB

PDF to FXLEPUB converts image-heavy PDFs or image folders into EPUB3 fixed-layout EPUB files for **Apple Books**.

It is meant for quickly converting lots of manga, picture books, artbooks, comics, manuals, booklets, scanned books, and similar visual PDFs with as little setup and manual editing as possible.

This is not a flexible text/layout converter like some other tools. It does not try to extract real text, rebuild complex layouts, run OCR, create chapters, or produce rich metadata. It treats pages as fixed visual pages because complex PDF layout conversion is difficult to make perfect across many different books.

I made this because Apple Books handles PDFs and EPUBs differently. In Apple Books, page curl animation is available for EPUBs, but not PDFs. I wanted that EPUB reading experience and page curl animation for all of my books in Apple Books, including books that originally came as PDFs.

## Transparency

This is a personal free-time vibe-coding project. Issues and feature requests may not be fixed quickly, or ever. If you need a change, you may need to fork the project and implement it yourself.

The code was generated and iterated with:

- Claude Sonnet 4.5
- OpenAI Codex based on GPT-5

The human project owner directed the requirements, tested behavior, chose the design decisions, and requested changes, but did not manually write any lines of source code.

## Limitations

- Made specifically for Apple Books. Other EPUB readers may display the output incorrectly, and non-Apple compatibility may not be fixed.
- Creates fixed-layout EPUBs only. Text is not reflowable, and font size cannot be changed like a normal text EPUB.
- Only English and Japanese are included as metadata language options because those are what the project owner mainly uses for digital books.
- Does not remove DRM and is not intended for piracy. Use it only with PDFs or images you have the legal right to convert. You are responsible for making sure your use is legal in your country.
- Developed mainly on Windows. macOS/Linux may work from the command line, but drag-and-drop behavior may differ.

## Requirements

- Python 3.10 or newer installed before use
- A modern browser for the preview page
- PyMuPDF
- Pillow

The script checks for PyMuPDF and Pillow at startup and tries to install missing dependencies automatically.

Manual dependency install:

```powershell
python -m pip install -r requirements.txt
```

## Usage

Drag a PDF file or image folder onto:

```text
pdf_to_fxlepub.py
```

Or run it from a terminal:

```powershell
python pdf_to_fxlepub.py "D:\Books\My Book.pdf"
```

The final `.epub` is created beside `pdf_to_fxlepub.py`. If a file with the same name already exists, a numbered filename is used instead.

If you want to add chapters, richer metadata, or make other EPUB edits after conversion, use an EPUB editor such as [Sigil](https://sigil-ebook.com/).

## Preview Features

Before packaging the EPUB, the script opens `preview.html` in your default browser.

From the preview, you can:

- Set title and creator.
- Choose English or Japanese metadata language.
- Choose LTR or RTL page direction.
- Enable or disable Apple Books `ibooks:binding`.
- Enable or disable all-landscape spread splitting when available.
- Add a blank second page to fix spread alignment.
- Hover over pages to add a blank page before them or remove them.
- Confirm, cancel, and watch real progress while the EPUB is packaged.

## Spreads And Page Handling

Double-width images are treated as two logical EPUB pages without modifying the original image. The EPUB uses CSS to show only the left or right half.

If every page is landscape and shares the same aspect ratio, the preview shows a **Split landscape spreads** switch. When enabled, each landscape image is treated as a double-page spread, and the first image is also used as a single-page cover on a black background.

If converted or extracted page images have dimensions that do not safely match the detected layout, the script stops and leaves `temp_images`. This is intentional: it gives you a chance to inspect, replace, resize, add, or remove images before continuing. After editing, drag the `temp_images` folder back onto the script.

This manual stop is a feature, not a conversion bug. The UX could be improved in the future, but the current behavior is meant to avoid silently creating a broken EPUB from mismatched pages.

## How It Works

1. Reads the PDF or image folder.
2. Extracts single-image-only PDF pages directly when possible.
3. Renders PDF pages with text, vector layout, or multiple visible objects to PNG.
4. Detects normal pages, double-width pages, and optional all-landscape spread books.
5. Opens the browser preview.
6. Writes XHTML, CSS, metadata, images, and packages the EPUB after confirmation.

## Troubleshooting

- **Page order looks wrong:** try switching LTR/RTL, then try **Add Blank 2nd Page**.
- **EPUB file is large:** fixed-layout image EPUBs are usually large.
- **MuPDF warnings appear:** some PDFs contain unusual internal objects. If the final EPUB looks correct, the warnings can usually be ignored.

## Acknowledgements

PDF to FXLEPUB is only possible because of open-source projects including:

- [PyMuPDF](https://pymupdf.readthedocs.io/) for reading, rendering, and extracting PDF content.
- [Pillow](https://python-pillow.org/) for image inspection and validation.
- [Python](https://www.python.org/) and its standard library for the rest of the workflow.

## License

This project is licensed under the GNU General Public License v3.0. See `LICENSE` for details.
