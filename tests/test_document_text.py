import io
import zipfile

import pytest

from control_plane.services import document_text as dt
from ui.state import WorkspaceStateStore

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'


def _zip(parts):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in parts.items():
            zf.writestr(name, body)
    return buf.getvalue()


def _docx():
    return _zip({"word/document.xml": f'<w:document {W}><w:body><w:p><w:r><w:t>Mapping rule: CustodianName</w:t></w:r></w:p>'
                                      f'<w:p><w:r><w:t>to CUSTODIAN_CODE</w:t></w:r></w:p></w:body></w:document>'})


def _pptx():
    return _zip({"ppt/slides/slide2.xml": f'<p:sld xmlns:p="x" {A}><a:p><a:r><a:t>Second</a:t></a:r></a:p></p:sld>',
                 "ppt/slides/slide1.xml": f'<p:sld xmlns:p="x" {A}><a:p><a:r><a:t>First</a:t></a:r></a:p></p:sld>'})


def _pdf(text="Source field RegistrationType"):
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 100] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    stream = f"BT /F1 12 Tf 10 50 Td ({text}) Tj ET"
    objs[3] = f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"
    out, offsets = "%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    return out.encode()


def test_docx_pptx_pdf_text_is_extracted_in_order():
    assert dt.docx_text(_docx()) == "Mapping rule: CustodianName\nto CUSTODIAN_CODE"
    assert dt.pptx_text(_pptx()) == "Slide 1\nFirst\nSlide 2\nSecond"
    assert "RegistrationType" in dt.pdf_text(_pdf())


@pytest.mark.parametrize("name,payload", [("a.docx", _docx), ("a.pptx", _pptx), ("a.pdf", _pdf)])
def test_knowledge_store_registers_office_and_pdf_documents(name, payload):
    store = WorkspaceStateStore()
    doc = store.add_firm_file(name, "business_rules", payload())
    assert doc["status"] == "REGISTERED_REVIEW_REQUIRED" and doc["full_text"].strip()


def test_wrong_or_corrupt_documents_are_rejected_clearly():
    with pytest.raises(ValueError):
        dt.docx_text(b"not a zip")
    with pytest.raises(ValueError):
        dt.docx_text(_pptx())
    with pytest.raises(ValueError):
        dt.pdf_text(b"not a pdf")
    with pytest.raises(ValueError, match="Could not parse"):
        WorkspaceStateStore().add_firm_file("bad.docx", "business_rules", b"junk")
