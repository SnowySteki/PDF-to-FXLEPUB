import html
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
import webbrowser
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

try:
    from PIL import Image
except ImportError:
    Image = None


INPUT_IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
}

EPUB_IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
}

REQUIRED_DEPENDENCIES = [
    {
        "module": "fitz",
        "package": "PyMuPDF",
        "requirement": "PyMuPDF>=1.23",
    },
    {
        "module": "PIL.Image",
        "package": "Pillow",
        "requirement": "Pillow>=10.0",
    },
]

DOUBLE_PAGE_WIDTH_TOLERANCE = 1
PAGE_ASPECT_RATIO_TOLERANCE = 0.005


class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    MAGENTA = "\033[95m"


def enable_ansi_colors():
    os.system("")


def color(text, code):
    return f"{code}{text}{Colors.RESET}"


def print_header(message):
    print()
    print(color("=" * 60, Colors.CYAN))
    print(color(message, Colors.BOLD + Colors.CYAN))
    print(color("=" * 60, Colors.CYAN))


def print_step(message):
    print(color(message, Colors.BLUE))


def print_info(message):
    print(color(message, Colors.CYAN))


def print_success(message):
    print(color(message, Colors.GREEN))


def print_warning(message):
    print(color(message, Colors.YELLOW))


def print_error(message):
    print(color(message, Colors.RED))


def pause(message="Press Enter to exit..."):
    try:
        input(f"\n{message}")
    except EOFError:
        pass


def ensure_dependencies():
    print_step("[STEP 0] Checking Python dependencies")
    missing_requirements = []

    for dependency in REQUIRED_DEPENDENCIES:
        try:
            importlib.import_module(dependency["module"])
        except ImportError:
            missing_requirements.append(dependency["requirement"])

    if missing_requirements:
        print_warning("Installing missing dependencies:")
        for requirement in missing_requirements:
            print_warning(f"  - {requirement}")
        install_dependencies(missing_requirements)
    else:
        print_success("Dependencies are installed")

    load_runtime_dependencies()


def install_dependencies(requirements):
    command = [sys.executable, "-m", "pip", "install", *requirements]
    try:
        subprocess.check_call(command)
    except subprocess.CalledProcessError:
        print_warning("pip install failed. Trying to enable or update pip first.")
        subprocess.check_call([sys.executable, "-m", "ensurepip", "--upgrade"])
        subprocess.check_call(command)


def load_runtime_dependencies():
    global fitz, Image

    fitz = importlib.import_module("fitz")
    Image = importlib.import_module("PIL.Image")


def natural_key(path):
    name = Path(path).name
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", name)]


def clean_folder(folder):
    folder = Path(folder)
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True, exist_ok=True)


def load_images_from_folder(folder_path):
    folder = Path(folder_path)
    image_paths = [
        path
        for path in folder.iterdir()
        if path.is_file() and path.suffix.lower() in INPUT_IMAGE_EXTENSIONS
    ]
    return sorted(image_paths, key=natural_key)


def get_displayed_image_info(page):
    try:
        image_infos = page.get_image_info(xrefs=True)
        return [info for info in image_infos if info.get("xref")]
    except Exception:
        displayed_images = []
        for image in page.get_images(full=True):
            xref = image[0]
            for bbox in page.get_image_rects(xref):
                displayed_images.append({"xref": xref, "bbox": bbox})
        return displayed_images


def page_has_text(page):
    return bool(page.get_text().strip())


def color_is_white(color_value):
    if color_value is None:
        return True
    return all(component >= 0.99 for component in color_value[:3])


def rect_covers_page(rect_value, page_rect, tolerance=1.0):
    if rect_value is None:
        return False

    rect = fitz.Rect(rect_value)
    return (
        rect.x0 <= page_rect.x0 + tolerance
        and rect.y0 <= page_rect.y0 + tolerance
        and rect.x1 >= page_rect.x1 - tolerance
        and rect.y1 >= page_rect.y1 - tolerance
    )


def drawing_is_plain_white_background(drawing, page_rect):
    fill = drawing.get("fill")
    stroke = drawing.get("color")
    has_fill = fill is not None and drawing.get("fill_opacity", 1) > 0
    has_stroke = stroke is not None and drawing.get("stroke_opacity", 1) > 0

    return (
        has_fill
        and color_is_white(fill)
        and color_is_white(stroke)
        and rect_covers_page(drawing.get("rect"), page_rect)
        and (not has_stroke or drawing.get("width", 0) <= 1)
    )


def page_has_significant_drawings(page):
    try:
        return any(
            not drawing_is_plain_white_background(drawing, page.rect)
            for drawing in page.get_drawings()
        )
    except Exception:
        return False


def page_is_single_image_only(page, displayed_images):
    return (
        len(displayed_images) == 1
        and not page_has_text(page)
        and not page_has_significant_drawings(page)
    )


def extract_single_displayed_image(doc, image_info, page_number, output_folder):
    base_image = doc.extract_image(image_info["xref"])
    ext = f".{base_image['ext'].lower()}"
    if ext not in EPUB_IMAGE_MEDIA_TYPES and ext not in INPUT_IMAGE_EXTENSIONS:
        return None

    output_path = Path(output_folder) / f"page_{page_number:04d}{ext}"
    output_path.write_bytes(base_image["image"])

    try:
        with Image.open(output_path) as image:
            image.verify()
    except Exception:
        output_path.unlink(missing_ok=True)
        return None

    return output_path


def render_page_to_png(page, page_number, output_folder):
    matrix = fitz.Matrix(4, 4)
    pixmap = page.get_pixmap(matrix=matrix, alpha=False)
    output_path = Path(output_folder) / f"page_{page_number:04d}.png"
    pixmap.save(output_path)
    return output_path


def extract_images_from_pdf(pdf_path, output_folder):
    print_step("[STEP 1] Extracting pages from PDF")
    clean_folder(output_folder)

    images = []
    extracted_count = 0
    converted_count = 0

    with fitz.open(pdf_path) as doc:
        total_pages = len(doc)
        print_info(f"PDF pages: {total_pages}")

        for index, page in enumerate(doc):
            page_number = index + 1
            output_path = None
            displayed_images = get_displayed_image_info(page)

            if page_is_single_image_only(page, displayed_images):
                output_path = extract_single_displayed_image(
                    doc,
                    displayed_images[0],
                    page_number,
                    output_folder,
                )

            if output_path:
                extracted_count += 1
                print_success(f"[Page {page_number}/{total_pages}] Extracted ({output_path.suffix[1:].lower()})")
            else:
                output_path = render_page_to_png(page, page_number, output_folder)
                converted_count += 1
                print_success(f"[Page {page_number}/{total_pages}] Converted (PNG 4x)")

            images.append(output_path)

    print_success(
        f"Processed {len(images)} pages: {extracted_count} extracted, {converted_count} converted"
    )
    return images


def get_image_dimensions(image_paths):
    dimensions = []
    for path in image_paths:
        with Image.open(path) as image:
            dimensions.append((Path(path), image.width, image.height))
    return dimensions


def print_dimension_report(dimensions):
    groups = defaultdict(list)
    for path, width, height in dimensions:
        groups[(width, height)].append(path)

    print_warning("Images have different dimensions:")
    for (width, height), paths in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0])):
        print_warning(f"  {width}x{height}: {len(paths)} file(s)")
        preview = paths[:10]
        for path in preview:
            print(f"    - {path.name}")
        if len(paths) > len(preview):
            print(f"    - ... {len(paths) - len(preview)} more")


def check_image_dimensions(image_paths):
    print_step("[STEP 2] Checking image dimensions")
    dimensions = get_image_dimensions(image_paths)

    if not dimensions:
        raise ValueError("No images found.")

    counts = Counter((width, height) for _, width, height in dimensions)
    if len(counts) == 1:
        width, height = next(iter(counts))
        print_success(f"All images match: {width}x{height}")
        return True, width, height

    print_dimension_report(dimensions)
    return False, None, None


def find_normal_page_size(dimensions, split_uniform_landscape=True):
    if not dimensions:
        raise ValueError("No images found.")

    if split_uniform_landscape:
        uniform_landscape_size = uniform_landscape_double_page_size(dimensions)
        if uniform_landscape_size:
            return uniform_landscape_size[0], uniform_landscape_size[1], []

    counts = Counter((width, height) for _, width, height in dimensions)
    if len(counts) == 1:
        width, height = next(iter(counts))
        if split_uniform_landscape and width > height:
            return double_page_half_width(width), height, []
        return width, height, []

    candidates = []

    for (normal_width, normal_height), occurrence_count in counts.items():
        invalid, double_count = page_size_candidate_result(
            dimensions,
            normal_width,
            normal_height,
        )
        candidates.append(
            {
                "normal_width": normal_width,
                "normal_height": normal_height,
                "occurrence_count": occurrence_count,
                "invalid": invalid,
                "double_count": double_count,
            }
        )

    double_candidates = [candidate for candidate in candidates if candidate["double_count"] > 0]
    if double_candidates:
        best = max(
            double_candidates,
            key=lambda candidate: (
                candidate["double_count"],
                -len(candidate["invalid"]),
                candidate["occurrence_count"],
                candidate["normal_width"],
                candidate["normal_height"],
            ),
        )
        return best["normal_width"], best["normal_height"], best["invalid"]

    best = min(
        candidates,
        key=lambda candidate: (
            len(candidate["invalid"]),
            -candidate["occurrence_count"],
            candidate["normal_width"],
            candidate["normal_height"],
        ),
    )
    return best["normal_width"], best["normal_height"], best["invalid"]


def uniform_landscape_double_page_size(dimensions):
    if not dimensions or not all(width > height for _, width, height in dimensions):
        return None

    counts = Counter((width, height) for _, width, height in dimensions)
    base_width, base_height = counts.most_common(1)[0][0]
    for _, width, height in dimensions:
        if not is_aspect_ratio_match(width, height, base_width, base_height):
            return None

    return double_page_half_width(base_width), base_height


def double_page_half_width(width):
    return (width + 1) // 2


def page_size_candidate_result(dimensions, normal_width, normal_height):
    invalid = []
    double_count = 0

    for path, width, height in dimensions:
        is_single = is_single_page_image(width, height, normal_width, normal_height)
        is_double = is_double_width_image(width, height, normal_width, normal_height)
        if is_double:
            double_count += 1
        elif not is_single:
            invalid.append((path, width, height))

    return invalid, double_count


def is_double_width_image(width, height, normal_width, normal_height):
    expected_width = normal_width * 2
    return (
        (
            height == normal_height
            and abs(width - expected_width) <= DOUBLE_PAGE_WIDTH_TOLERANCE
        )
        or is_aspect_ratio_match(width, height, expected_width, normal_height)
    )


def is_single_page_image(width, height, normal_width, normal_height):
    return (
        (width == normal_width and height == normal_height)
        or is_aspect_ratio_match(width, height, normal_width, normal_height)
    )


def is_aspect_ratio_match(width, height, target_width, target_height):
    if height == 0 or target_height == 0:
        return False
    return abs((width / height) - (target_width / target_height)) <= PAGE_ASPECT_RATIO_TOLERANCE


def epub_image_filename(index, source_path):
    ext = Path(source_path).suffix.lower()
    if ext == ".jpeg":
        ext = ".jpg"
    if ext not in EPUB_IMAGE_MEDIA_TYPES:
        ext = ".png"
    return f"page_{index:04d}{ext}"


def create_page_entries(image_paths, page_progression, split_uniform_landscape=True):
    print_step("[STEP 2] Checking image dimensions and double-page layout")
    dimensions = get_image_dimensions(image_paths)
    normal_width, normal_height, invalid = find_normal_page_size(
        dimensions,
        split_uniform_landscape=split_uniform_landscape,
    )

    print_info(f"Normal page size: {normal_width}x{normal_height}")
    print_info(f"Double-page width tolerance: +/-{DOUBLE_PAGE_WIDTH_TOLERANCE}px")
    print_info(f"Page aspect ratio tolerance: +/-{PAGE_ASPECT_RATIO_TOLERANCE}")
    if invalid:
        print_warning("Images that are neither normal/double-page size nor a matching aspect ratio:")
        for path, width, height in invalid:
            print(f"  - {path.name}: {width}x{height}")
        return None, None, None

    page_entries = []
    double_page_count = 0
    duplicate_first_page_as_cover = (
        split_uniform_landscape
        and uniform_landscape_double_page_size(dimensions) == (normal_width, normal_height)
    )

    if duplicate_first_page_as_cover:
        first_path, _, _ = dimensions[0]
        page_entries.append(
            {
                "image_file": epub_image_filename(1, first_path),
                "css_file": "cover-contain.css",
                "viewport_width": normal_width,
                "viewport_height": normal_height,
                "alt": f"{first_path.stem} cover",
            }
        )

    for image_index, (path, width, height) in enumerate(dimensions, start=1):
        image_file = epub_image_filename(image_index, path)

        if is_double_width_image(width, height, normal_width, normal_height):
            double_page_count += 1
            half_order = ["right", "left"] if page_progression == "rtl" else ["left", "right"]
            for half in half_order:
                page_entries.append(
                    {
                        "image_file": image_file,
                        "css_file": f"crop-{half}.css",
                        "viewport_width": normal_width,
                        "viewport_height": normal_height,
                        "alt": f"{path.stem} {half} half",
                    }
            )
            continue

        if is_single_page_image(width, height, normal_width, normal_height):
            viewport_width = normal_width
            viewport_height = normal_height
        else:
            viewport_width = width
            viewport_height = height

        page_entries.append(
            {
                "image_file": image_file,
                "css_file": "style.css",
                "viewport_width": viewport_width,
                "viewport_height": viewport_height,
                "alt": path.stem,
            }
        )

    if double_page_count:
        print_success(
            f"Detected {double_page_count} double-page image(s); generated {len(page_entries)} XHTML pages"
        )
        if duplicate_first_page_as_cover:
            print_info("First landscape page is also used as a single-page cover with black bars.")
        print_info(f"Double-page order: {'right then left' if page_progression == 'rtl' else 'left then right'}")
    else:
        print_success(f"All content pages are single-page images: {len(page_entries)} XHTML pages")

    return page_entries, normal_width, normal_height


def create_epub_structure(base_folder):
    print_step("[STEP 3] Creating EPUB folder structure")
    clean_folder(base_folder)

    meta_inf = Path(base_folder) / "META-INF"
    oebps = Path(base_folder) / "OEBPS"
    images_folder = oebps / "images"

    meta_inf.mkdir(parents=True, exist_ok=True)
    images_folder.mkdir(parents=True, exist_ok=True)
    return meta_inf, oebps, images_folder


def create_mimetype(base_folder):
    (Path(base_folder) / "mimetype").write_text("application/epub+zip", encoding="utf-8")


def create_container_xml(meta_inf_folder):
    content = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""
    (Path(meta_inf_folder) / "container.xml").write_text(content, encoding="utf-8", newline="\n")


def create_css(oebps_folder):
    normal_content = """@page {
  margin: 0;
  padding: 0;
}

html,
body {
  width: 100%;
  height: 100%;
  margin: 0;
  padding: 0;
}

body {
  overflow: hidden;
}

#page {
  width: 100%;
  height: 100%;
  margin: 0;
  padding: 0;
}

img {
  display: block;
  width: 100%;
  height: 100%;
  margin: 0;
  padding: 0;
  object-fit: contain;
}"""

    crop_base = """@page {
  margin: 0;
  padding: 0;
}

html,
body {
  width: 100%;
  height: 100%;
  margin: 0;
  padding: 0;
  overflow: hidden;
}

#page {
  position: relative;
  width: 100%;
  height: 100%;
  margin: 0;
  padding: 0;
  overflow: hidden;
}

img {
  position: absolute;
  top: 0;
  width: 200%;
  height: 100%;
  margin: 0;
  padding: 0;
  object-fit: fill;
}
"""

    cover_contain_content = """@page {
  margin: 0;
  padding: 0;
}

html,
body {
  width: 100%;
  height: 100%;
  margin: 0;
  padding: 0;
  background: #000000;
}

body {
  overflow: hidden;
}

#page {
  width: 100%;
  height: 100%;
  margin: 0;
  padding: 0;
  background: #000000;
}

img {
  display: block;
  width: 100%;
  height: 100%;
  margin: 0;
  padding: 0;
  object-fit: contain;
}"""

    (Path(oebps_folder) / "style.css").write_text(normal_content, encoding="utf-8", newline="\n")
    (Path(oebps_folder) / "cover-contain.css").write_text(
        cover_contain_content,
        encoding="utf-8",
        newline="\n",
    )
    (Path(oebps_folder) / "crop-left.css").write_text(
        crop_base + "\nimg {\n  left: 0;\n}\n",
        encoding="utf-8",
        newline="\n",
    )
    (Path(oebps_folder) / "crop-right.css").write_text(
        crop_base + "\nimg {\n  right: 0;\n}\n",
        encoding="utf-8",
        newline="\n",
    )


def create_nav_xhtml(oebps_folder):
    # EPUB3 requires a nav document; this keeps it hidden and empty.
    content = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head>
  <title>Navigation</title>
  <meta charset="UTF-8"/>
</head>
<body>
  <nav epub:type="toc" id="toc" hidden="hidden">
    <ol/>
  </nav>
</body>
</html>"""
    (Path(oebps_folder) / "nav.xhtml").write_text(content, encoding="utf-8", newline="\n")


def convert_image_to_png(source_path, output_path):
    with Image.open(source_path) as image:
        if image.mode in ("RGBA", "LA"):
            image.save(output_path, "PNG")
            return

        if image.mode == "P":
            image = image.convert("RGBA")
        elif image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGB")
        image.save(output_path, "PNG")


def copy_images_to_epub(image_paths, images_folder):
    print_step("[STEP 4] Copying images into EPUB")
    epub_images = []

    for index, source_path in enumerate(image_paths, start=1):
        source_path = Path(source_path)
        output_path = Path(images_folder) / epub_image_filename(index, source_path)

        if source_path.suffix.lower() in EPUB_IMAGE_MEDIA_TYPES:
            shutil.copyfile(source_path, output_path)
        else:
            convert_image_to_png(source_path, output_path)

        epub_images.append(output_path.name)

    print_success(f"Copied {len(epub_images)} image(s)")
    return epub_images


def create_xhtml_files(oebps_folder, page_entries):
    print_step("[STEP 5] Creating XHTML pages")
    for old_page in Path(oebps_folder).glob("page_*.xhtml"):
        old_page.unlink()

    spine_refs = []
    xhtml_index = 0

    for entry in page_entries:
        if entry.get("type") == "blank_nav":
            spine_refs.append("nav")
            continue

        xhtml_index += 1
        page_id = f"page_{xhtml_index:04d}"
        spine_refs.append(page_id)
        cover_type = ' epub:type="cover"' if xhtml_index == 1 else ""

        if entry.get("type") == "blank_page":
            content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head>
  <title>Page {xhtml_index}</title>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width={entry["viewport_width"]}, height={entry["viewport_height"]}"/>
  <link rel="stylesheet" type="text/css" href="style.css"/>
</head>
<body{cover_type}>
  <div id="page"></div>
</body>
</html>"""
        else:
            image_file = entry["image_file"]
            content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head>
  <title>Page {xhtml_index}</title>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width={entry["viewport_width"]}, height={entry["viewport_height"]}"/>
  <link rel="stylesheet" type="text/css" href="{entry["css_file"]}"/>
</head>
<body{cover_type}>
  <div id="page">
    <img src="images/{html.escape(image_file)}" alt="{html.escape(entry["alt"])}"/>
  </div>
</body>
</html>"""
        (Path(oebps_folder) / f"{page_id}.xhtml").write_text(content, encoding="utf-8", newline="\n")

    print_success(f"Created {xhtml_index} XHTML page(s)")
    return xhtml_index, spine_refs


def preview_page_html(entry, page_number):
    token = html.escape(entry["token"])
    controls = preview_page_controls_html()
    if entry.get("type") == "blank_nav":
        return f"""      <div class="page blank-nav" data-token="{token}" style="aspect-ratio: {entry["viewport_width"]} / {entry["viewport_height"]};">
        <span class="page-number">{page_number}</span>
{controls}
      </div>"""

    if entry.get("type") == "blank_page":
        return f"""      <div class="page blank-page" data-token="{token}" style="aspect-ratio: {entry["viewport_width"]} / {entry["viewport_height"]};">
        <span class="page-number">{page_number}</span>
{controls}
      </div>"""

    image_file = html.escape(entry["image_file"])
    alt_text = html.escape(entry["alt"])
    crop_class = ""
    if entry["css_file"] == "crop-left.css":
        crop_class = " crop-left"
    elif entry["css_file"] == "crop-right.css":
        crop_class = " crop-right"
    elif entry["css_file"] == "cover-contain.css":
        crop_class = " cover-contain"

    return f"""      <div class="page{crop_class}" data-token="{token}" style="aspect-ratio: {entry["viewport_width"]} / {entry["viewport_height"]};">
        <img src="epub_temp/OEBPS/images/{image_file}" alt="{alt_text}"/>
        <span class="page-number">{page_number}</span>
{controls}
      </div>"""


def preview_page_controls_html():
    return """        <div class="page-controls" aria-label="Page actions">
          <button type="button" title="Add blank page before this page" onclick="addBlankBefore(this)">Add</button>
          <button type="button" class="danger" title="Remove this page" onclick="removePreviewPage(this)">Remove</button>
        </div>"""


def assign_page_tokens(page_entries, prefix="source"):
    entries = []
    for index, entry in enumerate(page_entries, start=1):
        copied_entry = dict(entry)
        copied_entry["token"] = f"{prefix}-{index:04d}"
        entries.append(copied_entry)
    return entries


def page_entries_signature(page_entries):
    return tuple(
        (
            entry.get("type", "page"),
            entry.get("image_file"),
            entry.get("css_file"),
            entry.get("viewport_width"),
            entry.get("viewport_height"),
        )
        for entry in page_entries
    )


def preview_entries_with_optional_nav(page_entries, include_nav_second_page, width, height):
    preview_entries = list(page_entries)
    if include_nav_second_page and preview_entries:
        preview_entries.insert(
            1,
            {
                "type": "blank_nav",
                "token": "nav",
                "viewport_width": width,
                "viewport_height": height,
            },
        )
    return preview_entries


def build_final_page_entries(page_entries, page_sequence, width, height):
    token_map = {entry["token"]: entry for entry in page_entries}
    final_entries = []

    for token in page_sequence:
        if token in token_map:
            final_entries.append(token_map[token])
        elif token == "nav":
            final_entries.append(
                {
                    "type": "blank_nav",
                    "token": "nav",
                    "viewport_width": width,
                    "viewport_height": height,
                }
            )
        elif token.startswith("blank-"):
            final_entries.append(
                {
                    "type": "blank_page",
                    "token": token,
                    "viewport_width": width,
                    "viewport_height": height,
                }
            )

    if not final_entries:
        raise ValueError("Preview edits removed every page.")

    return final_entries


def preview_spread_html(page_entries, page_progression):
    spreads = []

    if page_entries:
        spreads.append([page_entries[0]])
        remaining_entries = page_entries[1:]
    else:
        remaining_entries = []

    for index in range(0, len(remaining_entries), 2):
        spreads.append(remaining_entries[index:index + 2])

    spread_class = "rtl" if page_progression == "rtl" else "ltr"
    spread_html = []
    current_page_number = 1
    for spread_number, spread_entries in enumerate(spreads, start=1):
        pages = []
        for entry in spread_entries:
            pages.append(preview_page_html(entry, current_page_number))
            current_page_number += 1
        spread_html.append(
            f"""    <section class="spread {spread_class}{' two-page' if len(spread_entries) > 1 else ''}" aria-label="Spread {spread_number}">
{chr(10).join(pages)}
    </section>"""
        )

    return "\n".join(spread_html), len(spreads)


def create_preview_html(
    script_dir,
    page_entries,
    page_progression,
    include_nav_second_page,
    width,
    height,
    landscape_split_available=False,
    landscape_split_off_entries=None,
    landscape_split_off_width=None,
    landscape_split_off_height=None,
):
    print_step("[STEP 6] Creating preview.html")
    preview_entries = preview_entries_with_optional_nav(
        page_entries,
        include_nav_second_page,
        width,
        height,
    )
    spread_class = "rtl" if page_progression == "rtl" else "ltr"
    split_on_html, split_on_spread_count = preview_spread_html(preview_entries, page_progression)
    split_off_html = ""
    split_off_width = landscape_split_off_width or width
    split_off_height = landscape_split_off_height or height
    if landscape_split_off_entries is not None:
        split_off_html, _ = preview_spread_html(
            preview_entries_with_optional_nav(
                landscape_split_off_entries,
                include_nav_second_page,
                split_off_width,
                split_off_height,
            ),
            page_progression,
        )
    landscape_split_control_html = ""
    if landscape_split_available:
        landscape_split_control_html = """      <div class="switch-row">
        <span>Split landscape spreads</span>
        <label class="switch" aria-label="Split landscape spreads">
          <input id="metadata-landscape-split" type="checkbox" checked="checked" onchange="setLandscapeSplitPreview()"/>
          <span class="switch-track"></span>
        </label>
      </div>
"""

    content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>EPUB Preview</title>
  <style>
    * {{
      box-sizing: border-box;
    }}

    :root {{
      color-scheme: dark;
      --bg: #08090b;
      --bg-elevated: rgba(34, 34, 38, 0.68);
      --bg-elevated-strong: rgba(44, 44, 48, 0.78);
      --stroke: rgba(255, 255, 255, 0.16);
      --stroke-strong: rgba(255, 255, 255, 0.26);
      --label: rgba(255, 255, 255, 0.94);
      --secondary-label: rgba(235, 235, 245, 0.62);
      --tertiary-label: rgba(235, 235, 245, 0.42);
      --accent: #0a84ff;
      --accent-pressed: #409cff;
      --danger: #ff453a;
      --shadow: rgba(0, 0, 0, 0.44);
    }}

    body {{
      margin: 0;
      min-height: 100vh;
      padding: 28px;
      background:
        linear-gradient(145deg, #0b0c10 0%, #15171d 48%, #07080b 100%);
      color: var(--label);
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", "Helvetica Neue", Arial, sans-serif;
      letter-spacing: 0;
    }}

    .header {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: baseline;
      margin-bottom: 22px;
    }}

    h1 {{
      margin: 0;
      font-size: 21px;
      font-weight: 650;
      letter-spacing: 0;
    }}

    .meta {{
      color: var(--secondary-label);
      font-size: 13px;
    }}

    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
      gap: 24px 28px;
      align-items: start;
      padding-bottom: 360px;
    }}

    .grid.ltr {{
      direction: ltr;
    }}

    .grid.rtl {{
      direction: rtl;
    }}

    .spread {{
      display: flex;
      gap: 0;
      justify-content: center;
      align-items: stretch;
      min-height: 120px;
      direction: ltr;
      position: relative;
    }}

    .spread.rtl {{
      flex-direction: row-reverse;
    }}

    body.binding-enabled .spread.two-page::after {{
      content: "";
      position: absolute;
      top: 0;
      bottom: 0;
      left: 50%;
      z-index: 3;
      width: 22px;
      pointer-events: none;
      transform: translateX(-50%);
      background:
        linear-gradient(
          90deg,
          rgba(0, 0, 0, 0.00) 0%,
          rgba(0, 0, 0, 0.18) 34%,
          rgba(255, 255, 255, 0.22) 50%,
          rgba(0, 0, 0, 0.22) 66%,
          rgba(0, 0, 0, 0.00) 100%
        );
      mix-blend-mode: multiply;
      opacity: 0.72;
    }}

    .page {{
      position: relative;
      overflow: hidden;
      background: #ffffff;
      border-radius: 4px;
      box-shadow: none;
      min-width: 0;
      flex: 1 1 0;
    }}

    .blank-nav {{
      background: #ffffff;
    }}

    .blank-page {{
      background: #ffffff;
    }}

    .cover-contain {{
      background: #000000;
    }}

    .spread .page:only-child {{
      max-width: 50%;
      flex: 0 1 50%;
    }}

    .page img {{
      display: block;
      width: 100%;
      height: 100%;
      object-fit: contain;
    }}

    .page.crop-left img,
    .page.crop-right img {{
      position: absolute;
      top: 0;
      width: 200%;
      height: 100%;
      max-width: none;
      object-fit: fill;
    }}

    .page.crop-left img {{
      left: 0;
    }}

    .page.crop-right img {{
      right: 0;
    }}

    .page-number {{
      position: absolute;
      right: 6px;
      bottom: 4px;
      min-width: 18px;
      padding: 2px 5px;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.72);
      color: #111111;
      font-size: 12px;
      font-weight: 650;
      text-align: center;
      box-shadow: 0 1px 5px rgba(0, 0, 0, 0.22);
      backdrop-filter: blur(12px) saturate(160%);
      -webkit-backdrop-filter: blur(12px) saturate(160%);
    }}

    .page-controls {{
      position: absolute;
      top: 6px;
      left: 6px;
      right: 6px;
      z-index: 4;
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 4px;
      opacity: 0;
      pointer-events: none;
      transition: opacity 120ms ease;
    }}

    .page:hover .page-controls,
    .page:focus-within .page-controls {{
      opacity: 1;
      pointer-events: auto;
    }}

    .page-controls button {{
      border: 1px solid rgba(255, 255, 255, 0.18);
      border-radius: 999px;
      padding: 5px 9px;
      background: rgba(32, 32, 36, 0.64);
      color: rgba(255, 255, 255, 0.94);
      font-size: 11px;
      font-weight: 650;
      cursor: pointer;
      box-shadow: 0 4px 14px rgba(0, 0, 0, 0.32);
      backdrop-filter: blur(18px) saturate(180%);
      -webkit-backdrop-filter: blur(18px) saturate(180%);
      transition:
        background 120ms ease,
        border-color 120ms ease,
        box-shadow 120ms ease,
        transform 80ms ease;
    }}

    .page-controls button:hover {{
      border-color: rgba(255, 255, 255, 0.30);
      background: rgba(54, 54, 60, 0.78);
      box-shadow: 0 6px 18px rgba(0, 0, 0, 0.38);
    }}

    .page-controls button:active {{
      transform: scale(0.96);
      background: rgba(20, 20, 24, 0.82);
      box-shadow: 0 2px 8px rgba(0, 0, 0, 0.30);
    }}

    .page-controls .danger {{
      background: rgba(255, 69, 58, 0.74);
    }}

    .page-controls .danger:hover {{
      background: rgba(255, 88, 80, 0.86);
    }}

    .page-controls .danger:active {{
      background: rgba(204, 48, 42, 0.88);
    }}

    .actions {{
      position: fixed;
      bottom: 16px;
      right: 16px;
      z-index: 20;
      display: grid;
      gap: 10px;
      width: 348px;
      padding: 12px;
      border: 1px solid var(--stroke);
      border-radius: 18px;
      background: linear-gradient(180deg, rgba(48, 48, 52, 0.74), rgba(24, 24, 28, 0.70));
      box-shadow:
        0 28px 70px rgba(0, 0, 0, 0.48),
        inset 0 1px 0 rgba(255, 255, 255, 0.14);
      backdrop-filter: blur(34px) saturate(170%);
      -webkit-backdrop-filter: blur(34px) saturate(170%);
    }}

    .metadata {{
      display: grid;
      gap: 9px;
    }}

    .metadata label {{
      display: grid;
      gap: 5px;
      color: var(--secondary-label);
      font-size: 12px;
      font-weight: 600;
    }}

    .metadata input[type="text"] {{
      width: 100%;
      border: 1px solid var(--stroke);
      border-radius: 10px;
      padding: 8px 10px;
      background: rgba(10, 10, 12, 0.42);
      color: var(--label);
      font-size: 13px;
      outline: none;
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.05);
    }}

    .metadata input[type="text"]:focus {{
      border-color: rgba(10, 132, 255, 0.72);
      box-shadow:
        0 0 0 3px rgba(10, 132, 255, 0.18),
        inset 0 1px 0 rgba(255, 255, 255, 0.06);
    }}

    .metadata .field {{
      display: grid;
      gap: 5px;
      color: var(--secondary-label);
      font-size: 12px;
      font-weight: 600;
    }}

    .buttons {{
      display: flex;
      gap: 8px;
    }}

    .actions button {{
      min-width: 92px;
      border: 0;
      border-radius: 10px;
      padding: 9px 12px;
      color: #ffffff;
      font-size: 14px;
      font-weight: 650;
      cursor: pointer;
      flex: 1 1 0;
      box-shadow:
        inset 0 1px 0 rgba(255, 255, 255, 0.18),
        0 8px 18px rgba(0, 0, 0, 0.26);
      transition:
        background 120ms ease,
        box-shadow 120ms ease,
        filter 120ms ease,
        transform 80ms ease;
    }}

    .actions button:hover {{
      filter: brightness(1.08);
      box-shadow:
        inset 0 1px 0 rgba(255, 255, 255, 0.24),
        0 10px 22px rgba(0, 0, 0, 0.32);
    }}

    .actions button:active {{
      transform: scale(0.97);
      filter: brightness(0.92);
      box-shadow:
        inset 0 1px 2px rgba(0, 0, 0, 0.18),
        0 4px 12px rgba(0, 0, 0, 0.28);
    }}

    .actions button:disabled {{
      cursor: default;
      opacity: 0.48;
      filter: saturate(0.65);
      transform: none;
      box-shadow:
        inset 0 1px 0 rgba(255, 255, 255, 0.08),
        0 4px 10px rgba(0, 0, 0, 0.20);
    }}

    .confirm {{
      background: linear-gradient(180deg, var(--accent-pressed), var(--accent));
    }}

    .cancel {{
      background: rgba(255, 69, 58, 0.78);
    }}

    .blank {{
      background: rgba(118, 118, 128, 0.34);
    }}

    .segmented {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 2px;
      padding: 3px;
      border: 1px solid rgba(255, 255, 255, 0.10);
      border-radius: 12px;
      background: rgba(118, 118, 128, 0.22);
      box-shadow: inset 0 1px 2px rgba(0, 0, 0, 0.28);
    }}

    .actions .segmented button {{
      min-width: 0;
      border: 0;
      border-radius: 9px;
      padding: 7px 8px;
      background: transparent;
      color: var(--secondary-label);
      font-size: 13px;
      font-weight: 650;
      box-shadow: none;
      cursor: pointer;
      transition:
        background 120ms ease,
        color 120ms ease,
        box-shadow 120ms ease,
        transform 80ms ease;
    }}

    .actions .segmented button:hover {{
      background: rgba(255, 255, 255, 0.10);
      color: var(--label);
      filter: none;
      box-shadow: none;
    }}

    .actions .segmented button:active {{
      transform: scale(0.96);
      filter: none;
      box-shadow: none;
    }}

    .actions .segmented button.active {{
      background: rgba(255, 255, 255, 0.20);
      color: var(--label);
      box-shadow:
        0 1px 8px rgba(0, 0, 0, 0.28),
        inset 0 1px 0 rgba(255, 255, 255, 0.20);
    }}

    .actions .segmented button.active:hover {{
      background: rgba(255, 255, 255, 0.26);
    }}

    .actions .segmented button.active:active {{
      background: rgba(255, 255, 255, 0.16);
    }}

    .switch-row {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      color: var(--secondary-label);
      font-size: 13px;
      font-weight: 600;
    }}

    .switch {{
      position: relative;
      display: inline-flex;
      width: 44px;
      height: 24px;
      flex: 0 0 auto;
      cursor: pointer;
    }}

    .switch input {{
      position: absolute;
      opacity: 0;
      pointer-events: none;
    }}

    .switch-track {{
      position: absolute;
      inset: 0;
      border-radius: 999px;
      background: rgba(118, 118, 128, 0.34);
      cursor: pointer;
      transition: background 120ms ease;
      box-shadow:
        inset 0 1px 3px rgba(0, 0, 0, 0.34),
        inset 0 0 0 1px rgba(255, 255, 255, 0.10);
      transition:
        background 120ms ease,
        box-shadow 120ms ease,
        filter 120ms ease;
    }}

    .switch:hover .switch-track {{
      filter: brightness(1.12);
      box-shadow:
        inset 0 1px 3px rgba(0, 0, 0, 0.34),
        inset 0 0 0 1px rgba(255, 255, 255, 0.20),
        0 0 0 3px rgba(255, 255, 255, 0.05);
    }}

    .switch:active .switch-track {{
      filter: brightness(0.92);
    }}

    .switch-track::after {{
      content: "";
      position: absolute;
      top: 3px;
      left: 3px;
      width: 18px;
      height: 18px;
      border-radius: 999px;
      background: #ffffff;
      box-shadow: 0 2px 8px rgba(0, 0, 0, 0.40);
      transition:
        transform 120ms ease,
        width 80ms ease;
    }}

    .switch:active .switch-track::after {{
      width: 21px;
    }}

    .switch input:checked + .switch-track {{
      background: linear-gradient(180deg, var(--accent-pressed), var(--accent));
    }}

    .switch input:checked + .switch-track::after {{
      transform: translateX(20px);
    }}

    .switch:active input:checked + .switch-track::after {{
      transform: translateX(17px);
    }}

    .progress-view {{
      position: fixed;
      inset: 0;
      z-index: 50;
      display: none;
      place-items: center;
      padding: 24px;
      background:
        linear-gradient(145deg, rgba(9, 10, 13, 0.96), rgba(18, 20, 26, 0.96));
      color: var(--label);
      text-align: center;
    }}

    body.progress-active .progress-view {{
      display: grid;
    }}

    body.progress-active .actions,
    body.progress-active .header,
    body.progress-active .grid {{
      display: none;
    }}

    .progress-panel {{
      width: min(560px, 100%);
      display: grid;
      gap: 16px;
      padding: 24px;
      border: 1px solid var(--stroke);
      border-radius: 22px;
      background: linear-gradient(180deg, rgba(48, 48, 52, 0.70), rgba(24, 24, 28, 0.66));
      box-shadow:
        0 28px 70px rgba(0, 0, 0, 0.48),
        inset 0 1px 0 rgba(255, 255, 255, 0.14);
      backdrop-filter: blur(34px) saturate(170%);
      -webkit-backdrop-filter: blur(34px) saturate(170%);
    }}

    .progress-panel h2 {{
      margin: 0;
      font-size: 22px;
      font-weight: 650;
    }}

    .progress-panel p {{
      margin: 0;
      color: var(--secondary-label);
      font-size: 14px;
      overflow-wrap: anywhere;
    }}

    .progress-bar {{
      height: 9px;
      overflow: hidden;
      border-radius: 999px;
      background: rgba(118, 118, 128, 0.24);
      box-shadow: inset 0 1px 2px rgba(0, 0, 0, 0.32);
    }}

    .progress-bar span {{
      display: block;
      width: 0%;
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, var(--accent), #64d2ff);
      transition: width 160ms ease;
    }}

    .progress-percent {{
      color: var(--label);
      font-size: 13px;
      font-weight: 650;
    }}

    .exit-button {{
      justify-self: center;
      min-width: 120px;
      border: 0;
      border-radius: 10px;
      padding: 10px 14px;
      background: linear-gradient(180deg, var(--accent-pressed), var(--accent));
      color: #ffffff;
      font-size: 14px;
      font-weight: 650;
      cursor: pointer;
      box-shadow:
        inset 0 1px 0 rgba(255, 255, 255, 0.18),
        0 8px 18px rgba(0, 0, 0, 0.26);
      transition:
        background 120ms ease,
        box-shadow 120ms ease,
        filter 120ms ease,
        transform 80ms ease;
    }}

    .exit-button:hover {{
      filter: brightness(1.08);
      box-shadow:
        inset 0 1px 0 rgba(255, 255, 255, 0.24),
        0 10px 22px rgba(0, 0, 0, 0.32);
    }}

    .exit-button:active {{
      transform: scale(0.97);
      filter: brightness(0.92);
      box-shadow:
        inset 0 1px 2px rgba(0, 0, 0, 0.18),
        0 4px 12px rgba(0, 0, 0, 0.28);
    }}

    .hidden {{
      display: none;
    }}

    @media (max-width: 520px) {{
      body {{
        padding: 16px;
      }}

      .header {{
        display: grid;
        gap: 6px;
      }}

      .grid {{
        grid-template-columns: 1fr;
        padding-bottom: 430px;
      }}

      .actions {{
        left: 12px;
        right: 12px;
        bottom: 12px;
        width: auto;
      }}
    }}

  </style>
  <script>
    let blankNavAdded = {str(include_nav_second_page).lower()};
    let blankPageCounter = 0;
    let currentProgression = "{page_progression}";
    let currentViewportWidth = {width};
    let currentViewportHeight = {height};
    let previewEdited = false;
    let statusTimer = null;
    const landscapeSplitAvailable = {str(landscape_split_available).lower()};
    const splitOnViewport = {{ width: {width}, height: {height} }};
    const splitOffViewport = {{ width: {split_off_width}, height: {split_off_height} }};

    function updatePreviewMeta(pageCount, spreadCount) {{
      const suffix = blankNavAdded ? " | blank nav.xhtml as page 2" : "";
      document.getElementById("preview-meta").textContent = pageCount + " pages | " + spreadCount + " spreads | " + currentProgression.toUpperCase() + suffix;
    }}

    function renumberPages(pages) {{
      pages.forEach((page, index) => {{
        const number = page.querySelector(".page-number");
        if (number) {{
          number.textContent = String(index + 1);
        }}
      }});
    }}

    function buildSpread(pages) {{
      const spread = document.createElement("section");
      spread.className = "spread " + currentProgression + (pages.length > 1 ? " two-page" : "");
      pages.forEach((page) => spread.appendChild(page));
      return spread;
    }}

    function setBindingPreview() {{
      const bindingSwitch = document.getElementById("metadata-binding");
      document.body.classList.toggle("binding-enabled", bindingSwitch.checked);
    }}

    function ensureBlankNavButton() {{
      if (document.getElementById("blank-nav-button")) {{
        return;
      }}
      const button = document.createElement("button");
      button.id = "blank-nav-button";
      button.className = "blank";
      button.textContent = "Add Blank 2nd Page";
      button.onclick = addBlankSecondPage;
      const confirmButton = document.querySelector(".confirm");
      confirmButton.before(button);
    }}

    function setLandscapeSplitPreview() {{
      if (!landscapeSplitAvailable) {{
        return;
      }}

      const splitSwitch = document.getElementById("metadata-landscape-split");
      if (previewEdited && !confirm("Changing spread splitting resets preview page edits.")) {{
        splitSwitch.checked = !splitSwitch.checked;
        return;
      }}

      const templateId = splitSwitch.checked ? "landscape-split-on-template" : "landscape-split-off-template";
      const template = document.getElementById(templateId);
      if (!template) {{
        return;
      }}

      const viewport = splitSwitch.checked ? splitOnViewport : splitOffViewport;
      currentViewportWidth = viewport.width;
      currentViewportHeight = viewport.height;
      previewEdited = false;
      blankNavAdded = false;
      ensureBlankNavButton();
      const grid = document.querySelector(".grid");
      grid.innerHTML = template.innerHTML;
      const pages = orderDoublePageHalves(currentPages());
      rebuildSpreads(pages);
      setBindingPreview();
    }}

    function rebuildSpreads(pages) {{
      const grid = document.querySelector(".grid");
      grid.textContent = "";
      grid.classList.toggle("rtl", currentProgression === "rtl");
      grid.classList.toggle("ltr", currentProgression !== "rtl");

      if (pages.length > 0) {{
        grid.appendChild(buildSpread([pages[0]]));
      }}

      for (let index = 1; index < pages.length; index += 2) {{
        grid.appendChild(buildSpread(pages.slice(index, index + 2)));
      }}

      renumberPages(pages);
      updatePreviewMeta(pages.length, grid.querySelectorAll(".spread").length);
    }}

    function halfPairInfo(firstPage, secondPage) {{
      if (!firstPage || !secondPage) {{
        return null;
      }}

      const firstImage = firstPage.querySelector("img");
      const secondImage = secondPage.querySelector("img");
      if (!firstImage || !secondImage || firstImage.getAttribute("src") !== secondImage.getAttribute("src")) {{
        return null;
      }}

      const firstIsLeft = firstPage.classList.contains("crop-left");
      const firstIsRight = firstPage.classList.contains("crop-right");
      const secondIsLeft = secondPage.classList.contains("crop-left");
      const secondIsRight = secondPage.classList.contains("crop-right");
      if (!((firstIsLeft && secondIsRight) || (firstIsRight && secondIsLeft))) {{
        return null;
      }}

      return {{
        left: firstIsLeft ? firstPage : secondPage,
        right: firstIsRight ? firstPage : secondPage,
      }};
    }}

    function orderDoublePageHalves(pages) {{
      const orderedPages = [];
      for (let index = 0; index < pages.length; index += 1) {{
        const pair = halfPairInfo(pages[index], pages[index + 1]);
        if (pair) {{
          if (currentProgression === "rtl") {{
            orderedPages.push(pair.right, pair.left);
          }} else {{
            orderedPages.push(pair.left, pair.right);
          }}
          index += 1;
        }} else {{
          orderedPages.push(pages[index]);
        }}
      }}
      return orderedPages;
    }}

    function setSegment(group, value) {{
      const input = document.getElementById("metadata-" + group);
      input.value = value;
      document.querySelectorAll('.segmented button[data-group="' + group + '"]').forEach((button) => {{
        const isActive = button.dataset.value === value;
        button.classList.toggle("active", isActive);
        button.setAttribute("aria-pressed", isActive ? "true" : "false");
      }});

      if (group === "progression") {{
        currentProgression = value === "rtl" ? "rtl" : "ltr";
        const pages = orderDoublePageHalves(currentPages());
        rebuildSpreads(pages);
      }}
    }}

    function pageControlsMarkup() {{
      return '<div class="page-controls" aria-label="Page actions">' +
        '<button type="button" title="Add blank page before this page" onclick="addBlankBefore(this)">Add</button>' +
        '<button type="button" class="danger" title="Remove this page" onclick="removePreviewPage(this)">Remove</button>' +
        '</div>';
    }}

    function createBlankPage(token) {{
      const blankPage = document.createElement("div");
      blankPage.className = "page blank-page";
      blankPage.dataset.token = token;
      blankPage.style.aspectRatio = currentViewportWidth + " / " + currentViewportHeight;
      blankPage.innerHTML = '<span class="page-number"></span>' + pageControlsMarkup();
      return blankPage;
    }}

    function createBlankToken() {{
      blankPageCounter += 1;
      return "blank-" + Date.now().toString(36) + "-" + blankPageCounter;
    }}

    function currentPages() {{
      return Array.from(document.querySelectorAll(".grid .page"));
    }}

    function collectPageSequence() {{
      return currentPages().map((page) => page.dataset.token);
    }}

    async function addBlankSecondPage() {{
      if (blankNavAdded) {{
        return;
      }}

      const button = document.getElementById("blank-nav-button");
      button.disabled = true;

      try {{
        await fetch("/blank-nav", {{ cache: "no-store" }});
      }} catch (error) {{
        document.body.dataset.previewError = "true";
        button.disabled = false;
        return;
      }}

      blankNavAdded = true;
      button.remove();

      const pages = currentPages();
      if (pages.length > 0) {{
        const blankPage = createBlankPage("nav");
        blankPage.className = "page blank-nav";
        pages.splice(1, 0, blankPage);
        previewEdited = true;
        rebuildSpreads(pages);
      }} else {{
        updatePreviewMeta(0, 0);
      }}
    }}

    function addBlankBefore(button) {{
      const pages = currentPages();
      const page = button.closest(".page");
      const pageIndex = pages.indexOf(page);
      if (pageIndex === -1) {{
        return;
      }}

      pages.splice(pageIndex, 0, createBlankPage(createBlankToken()));
      previewEdited = true;
      rebuildSpreads(pages);
    }}

    function removePreviewPage(button) {{
      const pages = currentPages();
      if (pages.length <= 1) {{
        alert("At least one page must remain.");
        return;
      }}

      const page = button.closest(".page");
      const pageIndex = pages.indexOf(page);
      if (pageIndex === -1) {{
        return;
      }}

      if (!confirm("Remove page " + (pageIndex + 1) + "?")) {{
        return;
      }}

      const removed = pages.splice(pageIndex, 1)[0];
      if (removed && removed.dataset.token === "nav") {{
        blankNavAdded = false;
        ensureBlankNavButton();
      }}

      previewEdited = true;
      rebuildSpreads(pages);
    }}

    function readMetadata() {{
      return new URLSearchParams({{
        title: document.getElementById("metadata-title").value.trim() || "Untitled",
        creator: document.getElementById("metadata-creator").value.trim() || "Unknown",
        language: document.getElementById("metadata-language").value,
        page_progression: document.getElementById("metadata-progression").value,
        ibooks_binding: document.getElementById("metadata-binding").checked ? "yes" : "no",
        landscape_split: document.getElementById("metadata-landscape-split")?.checked ? "yes" : "no",
        page_sequence: JSON.stringify(collectPageSequence()),
      }});
    }}

    function updateProgress(percent) {{
      const clampedPercent = Math.max(0, Math.min(100, Number(percent) || 0));
      const roundedPercent = Math.round(clampedPercent);
      const progressBar = document.getElementById("progress-bar");
      document.getElementById("progress-fill").style.width = roundedPercent + "%";
      document.getElementById("progress-percent").textContent = roundedPercent + "%";
      progressBar.setAttribute("aria-valuenow", String(roundedPercent));
    }}

    function showProgress(message) {{
      document.body.classList.add("progress-active");
      document.getElementById("progress-title").textContent = "Working";
      document.getElementById("progress-message").textContent = message;
      document.getElementById("progress-output").textContent = "";
      document.getElementById("progress-bar").classList.remove("hidden");
      document.getElementById("exit-button").classList.add("hidden");
      updateProgress(0);
    }}

    function showFinished(status) {{
      const done = status.state === "done";
      document.getElementById("progress-title").textContent = done ? "Conversion Complete" : "Conversion Stopped";
      document.getElementById("progress-message").textContent = status.message || "";
      document.getElementById("progress-output").textContent = status.output_path || "";
      updateProgress(done ? 100 : (status.progress || 0));
      document.getElementById("progress-bar").classList.toggle("hidden", !done);
      document.getElementById("exit-button").classList.remove("hidden");
      if (statusTimer) {{
        clearInterval(statusTimer);
        statusTimer = null;
      }}
    }}

    async function pollStatus() {{
      try {{
        const response = await fetch("/status", {{ cache: "no-store" }});
        const status = await response.json();
        if (status.state === "done" || status.state === "cancelled" || status.state === "error") {{
          showFinished(status);
          return;
        }}
        document.getElementById("progress-message").textContent = status.message || "Working...";
        updateProgress(status.progress);
      }} catch (error) {{
        document.getElementById("progress-message").textContent = "Waiting for converter status...";
      }}
    }}

    function startStatusPolling() {{
      pollStatus();
      statusTimer = setInterval(pollStatus, 800);
    }}

    async function exitPreview() {{
      try {{
        await fetch("/exit", {{ cache: "no-store" }});
      }} catch (error) {{
        document.body.dataset.previewError = "true";
      }}

      window.open("", "_self");
      window.close();

      setTimeout(() => {{
        document.getElementById("progress-title").textContent = "You can close this tab.";
        document.getElementById("progress-message").textContent = "";
        document.getElementById("exit-button").classList.add("hidden");
      }}, 500);
    }}

    async function finishPreview(decision) {{
      const buttons = document.querySelectorAll(".actions button");
      buttons.forEach((button) => button.disabled = true);

      try {{
        const options = {{ cache: "no-store" }};
        if (decision === "confirm") {{
          options.method = "POST";
          options.headers = {{ "Content-Type": "application/x-www-form-urlencoded" }};
          options.body = readMetadata();
        }}
        await fetch("/" + decision, options);
      }} catch (error) {{
        document.body.dataset.previewError = "true";
      }}

      showProgress(decision === "confirm" ? "Packaging EPUB..." : "Cancelling...");
      if (decision === "confirm") {{
        startStatusPolling();
      }} else {{
        showFinished({{ state: "cancelled", message: "Conversion cancelled.", output_path: "" }});
      }}
    }}
  </script>
</head>
<body class="binding-enabled">
  <div class="actions">
    <div class="metadata">
      <label>
        Title
        <input id="metadata-title" type="text" value="" placeholder="Untitled"/>
      </label>
      <label>
        Creator
        <input id="metadata-creator" type="text" value="" placeholder="Unknown"/>
      </label>
      <div class="field">
        <span>Language</span>
        <input id="metadata-language" type="hidden" value="en"/>
        <div class="segmented" role="group" aria-label="Language">
          <button type="button" data-group="language" data-value="en" class="active" aria-pressed="true" onclick="setSegment('language', 'en')">English</button>
          <button type="button" data-group="language" data-value="ja" aria-pressed="false" onclick="setSegment('language', 'ja')">Japanese</button>
        </div>
      </div>
      <div class="field">
        <span>Page Direction</span>
        <input id="metadata-progression" type="hidden" value="{page_progression}"/>
        <div class="segmented" role="group" aria-label="Page direction">
          <button type="button" data-group="progression" data-value="ltr" class="{'active' if page_progression == 'ltr' else ''}" aria-pressed="{'true' if page_progression == 'ltr' else 'false'}" onclick="setSegment('progression', 'ltr')">LTR</button>
          <button type="button" data-group="progression" data-value="rtl" class="{'active' if page_progression == 'rtl' else ''}" aria-pressed="{'true' if page_progression == 'rtl' else 'false'}" onclick="setSegment('progression', 'rtl')">RTL</button>
        </div>
      </div>
      <div class="switch-row">
        <span>iBooks binding line</span>
        <label class="switch" aria-label="iBooks binding line">
          <input id="metadata-binding" type="checkbox" checked="checked" onchange="setBindingPreview()"/>
          <span class="switch-track"></span>
        </label>
      </div>
{landscape_split_control_html.rstrip()}
    </div>
    <div class="buttons">
      <button class="cancel" onclick="finishPreview('cancel')">Cancel</button>
      {'<button id="blank-nav-button" class="blank" onclick="addBlankSecondPage()">Add Blank 2nd Page</button>' if not include_nav_second_page else ''}
      <button class="confirm" onclick="finishPreview('confirm')">Confirm</button>
    </div>
  </div>
  <header class="header">
    <h1>EPUB Preview</h1>
    <div id="preview-meta" class="meta">{len(preview_entries)} pages | {split_on_spread_count} spreads | {page_progression.upper()}{' | blank nav.xhtml as page 2' if include_nav_second_page else ''}</div>
  </header>
  <main class="grid {spread_class}">
{split_on_html}
  </main>
  <template id="landscape-split-on-template">
{split_on_html}
  </template>
  <template id="landscape-split-off-template">
{split_off_html}
  </template>
  <section class="progress-view" aria-live="polite">
    <div class="progress-panel">
      <h2 id="progress-title">Working</h2>
      <p id="progress-message">Packaging EPUB...</p>
      <div id="progress-bar" class="progress-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><span id="progress-fill"></span></div>
      <p id="progress-percent" class="progress-percent">0%</p>
      <p id="progress-output"></p>
      <button id="exit-button" class="exit-button hidden" onclick="exitPreview()">Exit</button>
    </div>
  </section>
</body>
</html>"""

    preview_path = Path(script_dir) / "preview.html"
    preview_path.write_text(content, encoding="utf-8", newline="\n")
    print_success(f"Preview created: {preview_path}")
    return preview_path


class PreviewRequestHandler(SimpleHTTPRequestHandler):
    directory_path = None
    decision_event = None
    exit_event = None
    decision = None
    include_nav_second_page = False
    metadata = None
    status = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(self.directory_path), **kwargs)

    def log_message(self, format_string, *args):
        return

    def do_GET(self):
        if self.path == "/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(json.dumps(type(self).status or {}).encode("utf-8"))
            return

        if self.path == "/exit":
            if type(self).exit_event:
                type(self).exit_event.set()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok")
            return

        if self.path == "/blank-nav":
            type(self).include_nav_second_page = True
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok")
            return

        if self.path in {"/confirm", "/cancel"}:
            type(self).decision = self.path.strip("/")
            type(self).decision_event.set()
            if self.path == "/cancel":
                type(self).status = {
                    "state": "cancelled",
                    "message": "Conversion cancelled.",
                    "output_path": "",
                    "progress": 0,
                }
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            messages = {
                "/confirm": "EPUB packaging confirmed. You can close this tab.",
                "/cancel": "EPUB packaging cancelled. You can close this tab.",
            }
            message = messages[self.path]
            self.wfile.write(
                f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <title>{html.escape(type(self).decision.title())}</title>
  <style>
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: #101010;
      color: #eeeeee;
      font-family: Arial, sans-serif;
    }}
  </style>
</head>
<body>
  <main>
    <h1>{html.escape(message)}</h1>
  </main>
</body>
</html>""".encode("utf-8")
            )
            return

        super().do_GET()

    def do_POST(self):
        if self.path == "/confirm":
            content_length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(content_length).decode("utf-8")
            values = parse_qs(body)
            type(self).metadata = {
                "title": values.get("title", ["Untitled"])[0].strip() or "Untitled",
                "creator": values.get("creator", ["Unknown"])[0].strip() or "Unknown",
                "language": values.get("language", ["en"])[0] if values.get("language", ["en"])[0] in {"ja", "en"} else "en",
                "page_progression": values.get("page_progression", ["ltr"])[0] if values.get("page_progression", ["ltr"])[0] in {"rtl", "ltr"} else "ltr",
                "ibooks_binding": values.get("ibooks_binding", ["yes"])[0] == "yes",
                "landscape_split": values.get("landscape_split", ["yes"])[0] == "yes",
            }
            try:
                page_sequence = json.loads(values.get("page_sequence", ["[]"])[0])
            except json.JSONDecodeError:
                page_sequence = []
            type(self).metadata["page_sequence"] = [
                token for token in page_sequence
                if isinstance(token, str)
            ]
            type(self).decision = "confirm"
            type(self).status = {
                "state": "working",
                "message": "Preparing EPUB packaging...",
                "output_path": "",
                "progress": 0,
            }
            type(self).decision_event.set()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok")
            return

        self.send_error(404)


def confirm_preview(preview_path, script_dir):
    print_header("Preview")
    decision_event = threading.Event()
    exit_event = threading.Event()
    PreviewRequestHandler.directory_path = Path(script_dir)
    PreviewRequestHandler.decision_event = decision_event
    PreviewRequestHandler.exit_event = exit_event
    PreviewRequestHandler.decision = None
    PreviewRequestHandler.include_nav_second_page = False
    PreviewRequestHandler.metadata = None
    PreviewRequestHandler.status = {
        "state": "preview",
        "message": "Waiting for preview confirmation.",
        "output_path": "",
        "progress": 0,
    }

    server = ThreadingHTTPServer(("127.0.0.1", 0), PreviewRequestHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    preview_url = f"http://127.0.0.1:{server.server_port}/{preview_path.name}"
    print_info(f"Opening preview in your default browser: {preview_url}")
    print_info("Use the preview page buttons to choose direction, confirm, cancel, add blank pages, or remove pages.")
    if not webbrowser.open(preview_url):
        print_warning(f"Could not open browser automatically. Open this URL manually: {preview_url}")

    decision_event.wait()
    return (
        PreviewRequestHandler.decision,
        PreviewRequestHandler.include_nav_second_page,
        PreviewRequestHandler.metadata,
        server,
        exit_event,
    )


def set_preview_status(state, message, output_path="", progress=None):
    if progress is None:
        progress = 100 if state == "done" else 0
    PreviewRequestHandler.status = {
        "state": state,
        "message": message,
        "output_path": str(output_path) if output_path else "",
        "progress": max(0, min(100, int(progress))),
    }


def wait_for_browser_exit(server, exit_event):
    print_info("Waiting for the browser Exit button...")
    exit_event.wait()
    server.shutdown()
    server.server_close()


def package_progress_callback(written_count, total_files, path):
    if total_files <= 0:
        progress = 90
    else:
        progress = 30 + int((written_count / total_files) * 60)
    set_preview_status(
        "working",
        f"Packaging EPUB file... ({written_count}/{total_files})",
        progress=progress,
    )


def media_type_for_image(filename):
    ext = Path(filename).suffix.lower()
    if ext not in EPUB_IMAGE_MEDIA_TYPES:
        raise ValueError(f"Unsupported EPUB image type: {filename}")
    return EPUB_IMAGE_MEDIA_TYPES[ext]


def create_content_opf(
    oebps_folder,
    image_files,
    xhtml_count,
    spine_refs,
    width,
    height,
    title,
    creator,
    language,
    page_progression,
    ibooks_binding,
):
    print_step("[STEP 7] Creating content.opf")
    book_id = f"urn:uuid:{uuid.uuid4()}"
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    title_xml = html.escape(title, quote=False)
    creator_xml = html.escape(creator, quote=False)

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="BookID" prefix="rendition: http://www.idpf.org/vocab/rendition/# ibooks: http://vocabulary.itunes.apple.com/rdf/ibooks/vocabulary-extensions-1.0/">',
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">',
        f'    <dc:identifier id="BookID">{book_id}</dc:identifier>',
        f"    <dc:title>{title_xml}</dc:title>",
        f"    <dc:creator>{creator_xml}</dc:creator>",
        f"    <dc:language>{language}</dc:language>",
        f'    <meta property="dcterms:modified">{modified}</meta>',
        '    <meta property="rendition:layout">pre-paginated</meta>',
        '    <meta property="rendition:orientation">auto</meta>',
        '    <meta property="rendition:spread">auto</meta>',
        '    <meta name="cover" content="img_0001"/>',
    ]

    binding_value = "true" if ibooks_binding else "false"
    lines.append(f"    <meta property=\"ibooks:binding\">{binding_value}</meta>")

    lines.extend(
        [
            "  </metadata>",
            "  <manifest>",
            '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
            '    <item id="css" href="style.css" media-type="text/css"/>',
            '    <item id="css_cover_contain" href="cover-contain.css" media-type="text/css"/>',
            '    <item id="css_crop_left" href="crop-left.css" media-type="text/css"/>',
            '    <item id="css_crop_right" href="crop-right.css" media-type="text/css"/>',
        ]
    )

    for index in range(1, xhtml_count + 1):
        lines.append(
            f'    <item id="page_{index:04d}" href="page_{index:04d}.xhtml" media-type="application/xhtml+xml"/>'
        )

    for index, image_file in enumerate(image_files, start=1):
        properties = ' properties="cover-image"' if index == 1 else ""
        lines.append(
            f'    <item id="img_{index:04d}" href="images/{html.escape(image_file)}" media-type="{media_type_for_image(image_file)}"{properties}/>'
        )

    lines.extend(
        [
            "  </manifest>",
            f'  <spine page-progression-direction="{page_progression}">',
        ]
    )

    for idref in spine_refs:
        lines.append(f'    <itemref idref="{idref}"/>')

    lines.extend(["  </spine>", "</package>"])
    (Path(oebps_folder) / "content.opf").write_text("\n".join(lines), encoding="utf-8", newline="\n")

    print_info(f"Title: {title} | Creator: {creator} | Language: {language}")
    print_info(f"Page size: {width}x{height} | Progression: {page_progression}")
    print_success("content.opf created")


def package_epub(base_folder, output_path, progress_callback=None):
    print_step("[STEP 8] Packaging EPUB")
    base_folder = Path(base_folder)
    output_path = Path(output_path)
    files_to_package = [
        path for path in sorted(base_folder.rglob("*"))
        if path.is_file() and path.name not in {"mimetype", "preview.html"}
    ]
    total_files = len(files_to_package) + 1

    with zipfile.ZipFile(output_path, "w") as epub:
        mimetype_path = base_folder / "mimetype"
        epub.write(mimetype_path, "mimetype", compress_type=zipfile.ZIP_STORED)
        if progress_callback:
            progress_callback(1, total_files, mimetype_path)

        for written_count, path in enumerate(files_to_package, start=2):
            arcname = path.relative_to(base_folder).as_posix()
            epub.write(path, arcname, compress_type=zip_compression_for_path(path))
            if progress_callback:
                progress_callback(written_count, total_files, path)

    size_mb = output_path.stat().st_size / (1024 * 1024)
    print_success(f"EPUB created: {output_path}")
    print_info(f"File size: {size_mb:.2f} MB")


def zip_compression_for_path(path):
    if Path(path).suffix.lower() in EPUB_IMAGE_MEDIA_TYPES:
        return zipfile.ZIP_STORED
    return zipfile.ZIP_DEFLATED


def unique_output_path(script_dir, base_name):
    output_path = Path(script_dir) / f"{base_name}.epub"
    counter = 1
    while output_path.exists():
        output_path = Path(script_dir) / f"{base_name}_{counter}.epub"
        counter += 1
    return output_path


def stop_for_manual_dimension_fix(source_label, image_folder):
    print_error("Stopping before EPUB creation.")
    print_info(f"Images are available here: {image_folder}")
    print_info("Fix the image dimensions manually, then drag that image folder onto this script to continue.")
    print_info(f"Source: {source_label}")
    pause()
    sys.exit(0)


def prepare_input(input_path, script_dir):
    input_path = Path(input_path)
    temp_images_folder = Path(script_dir) / "temp_images"

    if input_path.is_file() and input_path.suffix.lower() == ".pdf":
        print_header("PDF to Fixed-Layout EPUB Converter")
        image_paths = extract_images_from_pdf(input_path, temp_images_folder)
        return image_paths, input_path.stem, temp_images_folder, temp_images_folder

    if input_path.is_dir():
        print_header("Image Folder to Fixed-Layout EPUB Converter")
        image_paths = load_images_from_folder(input_path)
        if not image_paths:
            raise ValueError("No supported images found in the folder.")
        print_info(f"Found {len(image_paths)} image(s)")
        return image_paths, input_path.name, None, input_path

    raise ValueError("Input must be a PDF file or a folder containing images.")


def build_epub(input_path):
    script_dir = Path(__file__).resolve().parent
    image_paths, base_name, cleanup_folder, manual_fix_folder = prepare_input(input_path, script_dir)

    preview_page_progression = "ltr"
    split_page_entries, split_width, split_height = create_page_entries(
        image_paths,
        preview_page_progression,
        split_uniform_landscape=True,
    )
    if split_page_entries is None:
        stop_for_manual_dimension_fix(base_name, manual_fix_folder)
    split_page_entries = assign_page_tokens(split_page_entries, "split-source")

    normal_page_entries, normal_width, normal_height = create_page_entries(
        image_paths,
        preview_page_progression,
        split_uniform_landscape=False,
    )
    if normal_page_entries is not None:
        normal_page_entries = assign_page_tokens(normal_page_entries, "normal-source")

    landscape_split_available = (
        normal_page_entries is not None
        and (
            (split_width, split_height, page_entries_signature(split_page_entries))
            != (normal_width, normal_height, page_entries_signature(normal_page_entries))
        )
    )

    epub_work_folder = script_dir / "epub_temp"
    meta_inf, oebps, images_folder = create_epub_structure(epub_work_folder)

    create_mimetype(epub_work_folder)
    create_container_xml(meta_inf)
    create_css(oebps)
    create_nav_xhtml(oebps)
    epub_image_files = copy_images_to_epub(image_paths, images_folder)
    preview_path = create_preview_html(
        script_dir,
        split_page_entries,
        preview_page_progression,
        False,
        split_width,
        split_height,
        landscape_split_available,
        normal_page_entries if landscape_split_available else None,
        normal_width if landscape_split_available else None,
        normal_height if landscape_split_available else None,
    )
    preview_decision, include_nav_second_page, metadata, preview_server, preview_exit_event = confirm_preview(preview_path, script_dir)
    if preview_decision != "confirm":
        print_info(f"Preview files were left here: {epub_work_folder}")
        print_info(f"Preview page was left here: {preview_path}")
        if cleanup_folder:
            print_info(f"Editable extracted images were left here: {cleanup_folder}")
        wait_for_browser_exit(preview_server, preview_exit_event)
        sys.exit(0)

    title = metadata["title"]
    creator = metadata["creator"]
    language = metadata["language"]
    page_progression = metadata["page_progression"]
    ibooks_binding = metadata["ibooks_binding"]
    use_landscape_split = metadata.get("landscape_split", True)
    if use_landscape_split or not landscape_split_available:
        page_entries = split_page_entries
        width = split_width
        height = split_height
    else:
        page_entries = normal_page_entries
        width = normal_width
        height = normal_height

    try:
        set_preview_status("working", "Creating XHTML pages...", progress=10)
        final_page_entries = build_final_page_entries(
            page_entries,
            metadata.get("page_sequence") or [entry["token"] for entry in page_entries],
            width,
            height,
        )
        xhtml_count, spine_refs = create_xhtml_files(oebps, final_page_entries)
        set_preview_status("working", "Writing EPUB metadata...", progress=20)
        create_content_opf(
            oebps,
            epub_image_files,
            xhtml_count,
            spine_refs,
            width,
            height,
            title,
            creator,
            language,
            page_progression,
            ibooks_binding,
        )

        output_path = unique_output_path(script_dir, base_name)
        set_preview_status("working", "Packaging EPUB file...", progress=30)
        package_epub(epub_work_folder, output_path, package_progress_callback)

        set_preview_status("working", "Cleaning temporary files...", progress=95)
        shutil.rmtree(epub_work_folder)
        if cleanup_folder and Path(cleanup_folder).exists():
            shutil.rmtree(cleanup_folder)
        if preview_path.exists():
            preview_path.unlink()

        set_preview_status("done", "EPUB created successfully.", output_path, progress=100)
        wait_for_browser_exit(preview_server, preview_exit_event)
        return output_path
    except Exception as error:
        set_preview_status("error", f"Conversion failed: {error}", progress=0)
        wait_for_browser_exit(preview_server, preview_exit_event)
        raise


def main():
    enable_ansi_colors()

    if len(sys.argv) < 2:
        print("Usage: Drag a PDF file or image folder onto this script.")
        pause()
        sys.exit(1)

    try:
        ensure_dependencies()
        output_path = build_epub(sys.argv[1])
        print_header("Conversion Complete")
        print_success(f"Output: {output_path}")
    except KeyboardInterrupt:
        print_error("\nOperation cancelled.")
        pause()
        sys.exit(130)
    except Exception as error:
        print_error(f"\nFatal error: {error}")
        import traceback

        traceback.print_exc()
        pause()
        sys.exit(1)


if __name__ == "__main__":
    main()
