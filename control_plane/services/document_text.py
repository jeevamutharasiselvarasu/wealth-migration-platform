"""Plain-text extraction for narrative knowledge documents (Word, PowerPoint, PDF).

DOCX and PPTX are zip packages of XML and are read with the standard library only; PDF needs pypdf. Nothing here
executes document content: macros, embedded objects and scripts are never run, and only visible text is returned."""
from __future__ import annotations

import io
import re
import zipfile
from typing import List
from xml.etree import ElementTree as ET

DOCUMENT_SUFFIXES = {".docx", ".pptx", ".pdf"}
MAX_ZIP_PART_BYTES = 20_000_000
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _part(zf: zipfile.ZipFile, name: str) -> ET.Element:
    info = zf.getinfo(name)
    if info.file_size > MAX_ZIP_PART_BYTES:
        raise ValueError(f"Document part {name} is too large to read.")
    return ET.fromstring(zf.read(name))


def _open_zip(content: bytes) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise ValueError("The file is not a valid Office document.") from exc


def docx_text(content: bytes) -> str:
    with _open_zip(content) as zf:
        if "word/document.xml" not in zf.namelist():
            raise ValueError("The file is not a Word document.")
        root = _part(zf, "word/document.xml")
    lines: List[str] = []
    for para in root.iter(_W + "p"):
        text = "".join(t.text or "" for t in para.iter(_W + "t")).strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


def pptx_text(content: bytes) -> str:
    with _open_zip(content) as zf:
        slides = sorted((n for n in zf.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                        key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)))
        if not slides:
            raise ValueError("The file is not a PowerPoint presentation.")
        out: List[str] = []
        for number, name in enumerate(slides, 1):
            root = _part(zf, name)
            out.append(f"Slide {number}")
            for para in root.iter(_A + "p"):
                text = "".join(t.text or "" for t in para.iter(_A + "t")).strip()
                if text:
                    out.append(text)
    return "\n".join(out)


def pdf_text(content: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ValueError("PDF reading needs the pypdf package; install requirements.txt.") from exc
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            raise ValueError("Encrypted PDF files cannot be read.")
        return "\n".join((page.extract_text() or "").strip() for page in reader.pages).strip()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("The file is not a readable PDF.") from exc


def pdf_pages(content: bytes) -> List[str]:
    """The text of each page (for citing a page number)."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ValueError("PDF reading needs the pypdf package; install requirements.txt.") from exc
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            raise ValueError("Encrypted PDF files cannot be read.")
        return [(page.extract_text() or "").strip() for page in reader.pages]
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("The file is not a readable PDF.") from exc


def extract(suffix: str, content: bytes) -> str:
    return {".docx": docx_text, ".pptx": pptx_text, ".pdf": pdf_text}[suffix](content)
