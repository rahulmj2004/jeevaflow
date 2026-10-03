"""
PDF active-content detection (app/security/pdf_active_content.py)
and its place in the ingestion pipeline. All documents are synthetic
and built in-process, except fixtures/synthetic_report_c2pa.pdf: a
static synthetic report carrying a C2PA Content Credentials manifest,
which used to be rejected as "active content".
"""

import io
from pathlib import Path

import pymupdf
import pytest
from PIL import Image

from app import vault
from app.database import SessionLocal
from app.models import AuditEvent, Document
from app.security.pdf_active_content import scan_bytes
from app.storage import FileValidationError

from tests.conftest import INITIAL_REPORT_TEXT, ingest, make_pdf
from tests.test_security import consent_via_portal, send_demo, verify_portal


FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_report_c2pa.pdf"


# ============================================================
# BUILDERS
# ============================================================

def build_pdf(modify=None, text: str = INITIAL_REPORT_TEXT) -> bytes:
    """
    A valid PDF (correct xref, nothing hand-patched) with optional
    extra objects added through the PDF object API.
    """

    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), text, fontsize=11)

    if modify:
        modify(document, page)

    content = document.tobytes(no_new_id=True, deflate=True)
    document.close()
    return content


def new_object(document, source: str) -> int:
    xref = document.get_new_xref()
    document.update_object(xref, source)
    return xref


def add_annotation(document, page, source: str):
    xref = new_object(document, source)
    document.xref_set_key(page.xref, "Annots", f"[{xref} 0 R]")


def jumbf_manifest(body: bytes = b"synthetic-c2pa-claim") -> bytes:
    label = b"c2pa\x00"
    jumd_payload = b"c2pa" + bytes.fromhex("0011001080000aa00038" + "9b71") + b"\x03" + label
    jumd = (8 + len(jumd_payload)).to_bytes(4, "big") + b"jumd" + jumd_payload
    inner = (8 + len(body)).to_bytes(4, "big") + b"json" + body
    content = jumd + inner
    return (8 + len(content)).to_bytes(4, "big") + b"jumb" + content


def attach(document, payload: bytes, name="Content Credentials", relationship="C2PA_Manifest",
           mime="application/c2pa"):
    stream = new_object(document, "<</Type/EmbeddedFile>>")
    document.update_stream(stream, payload)
    filespec = new_object(
        document,
        f"<</Type/Filespec/F({name})/UF({name})/AFRelationship/{relationship}"
        f"/Subtype({mime})/EF<</F {stream} 0 R>>>>",
    )
    catalog = document.pdf_catalog()
    document.xref_set_key(catalog, "AF", f"[{filespec} 0 R]")
    document.xref_set_key(catalog, "Names", f"<</EmbeddedFiles<</Names[({name}) {filespec} 0 R]>>>>")


def png_bytes() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (120, 80), (200, 30, 30)).save(out, format="PNG")
    return out.getvalue()


def features(result) -> set:
    return {item.feature for item in result.findings}


# ============================================================
# ACCEPT: static documents
# ============================================================

def test_plain_static_pdf_is_accepted():
    result = scan_bytes(make_pdf(INITIAL_REPORT_TEXT))

    assert result.safe, result.to_dict()
    assert result.to_dict()["verdict"] == "ACCEPT"


def test_reported_synthetic_report_with_c2pa_manifest_is_accepted():
    result = scan_bytes(FIXTURE.read_bytes())

    assert result.safe, result.to_dict()
    assert [item.feature for item in result.allowed] == ["C2PA_MANIFEST"]


def test_generated_c2pa_manifest_is_accepted():
    result = scan_bytes(build_pdf(lambda doc, page: attach(doc, jumbf_manifest())))

    assert result.safe, result.to_dict()
    assert [item.feature for item in result.allowed] == ["C2PA_MANIFEST"]


def test_ordinary_metadata_fonts_images_links_and_outline_are_accepted():
    def modify(document, page):
        document.set_metadata({
            "title": "Synthetic report", "author": "Synthetic Lab",
            "producer": "Synthetic Producer", "creator": "Synthetic Creator",
        })
        document.set_xml_metadata(
            '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf='
            '"http://www.w3.org/1999/02/22-rdf-syntax-ns#"/></x:xmpmeta>'
        )
        page.insert_font(fontname="F9", fontbuffer=pymupdf.Font("cour").buffer)
        page.insert_text((72, 200), "Embedded font text", fontname="F9")
        page.insert_text((72, 230), "Mentions /JS and /JavaScript and /Launch as plain text")
        page.insert_image(pymupdf.Rect(72, 260, 192, 340), stream=png_bytes())
        page.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(72, 360, 200, 380),
                          "uri": "https://example.org/synthetic"})
        document.set_toc([[1, "Results", 1]])
        document.xref_set_key(document.pdf_catalog(), "OpenAction", f"[{page.xref} 0 R /Fit]")

    result = scan_bytes(build_pdf(modify))

    assert result.safe, result.to_dict()


def test_open_action_goto_is_accepted():
    def modify(document, page):
        document.xref_set_key(
            document.pdf_catalog(), "OpenAction", f"<</S/GoTo/D[{page.xref} 0 R /Fit]>>"
        )

    assert scan_bytes(build_pdf(modify)).safe


# ============================================================
# REJECT: JavaScript
# ============================================================

def test_open_action_javascript_is_rejected():
    def modify(document, page):
        document.xref_set_key(document.pdf_catalog(), "OpenAction", "<</S/JavaScript/JS(app.alert(1))>>")

    result = scan_bytes(build_pdf(modify))

    assert not result.safe
    assert {"JAVASCRIPT", "OPEN_ACTION"} <= features(result)
    assert result.to_dict()["verdict"] == "REJECT"


def test_document_level_javascript_name_tree_is_rejected():
    def modify(document, page):
        action = new_object(document, "<</S/JavaScript/JS(app.alert(1))>>")
        document.xref_set_key(document.pdf_catalog(), "Names", f"<</JavaScript<</Names[(a) {action} 0 R]>>>>")

    assert "JAVASCRIPT" in features(scan_bytes(build_pdf(modify)))


def test_link_annotation_javascript_is_rejected():
    def modify(document, page):
        add_annotation(document, page, "<</Type/Annot/Subtype/Link/Rect[0 0 50 50]"
                                       "/A<</S/JavaScript/JS(this.print())>>>>")

    assert "JAVASCRIPT" in features(scan_bytes(build_pdf(modify)))


def test_name_escaped_javascript_is_rejected():
    raw = make_pdf("hello").replace(
        b"/Type/Catalog", b"/Type/Catalog/OpenAction<</S/J#61vaScript/J#53(app.alert(1))>>", 1
    )

    assert b"/JavaScript" not in raw
    assert "JAVASCRIPT" in features(scan_bytes(raw))


# ============================================================
# REJECT: launch and other dangerous actions
# ============================================================

def test_open_action_launch_is_rejected():
    def modify(document, page):
        document.xref_set_key(document.pdf_catalog(), "OpenAction", "<</S/Launch/F(cmd.exe)>>")

    result = scan_bytes(build_pdf(modify))

    assert {"LAUNCH_ACTION", "OPEN_ACTION"} <= features(result)


def test_link_launch_action_is_rejected():
    def modify(document, page):
        add_annotation(document, page, "<</Type/Annot/Subtype/Link/Rect[0 0 50 50]"
                                       "/A<</S/Launch/Win<</F(calc.exe)>>>>>>")

    assert "LAUNCH_ACTION" in features(scan_bytes(build_pdf(modify)))


def test_additional_actions_are_rejected():
    def modify(document, page):
        document.xref_set_key(page.xref, "AA", "<</O<</S/URI/URI(https://example.org/beacon)>>>>")

    result = scan_bytes(build_pdf(modify))

    assert features(result) == {"ADDITIONAL_ACTIONS"}
    assert result.primary.object_type == "Page"


def test_open_action_uri_is_rejected():
    def modify(document, page):
        document.xref_set_key(document.pdf_catalog(), "OpenAction", "<</S/URI/URI(https://example.org)>>")

    assert features(scan_bytes(build_pdf(modify))) == {"OPEN_ACTION"}


@pytest.mark.parametrize("action", ["SubmitForm", "ImportData", "GoToR", "GoToE"])
def test_remote_actions_are_rejected(action):
    def modify(document, page):
        add_annotation(document, page, f"<</Type/Annot/Subtype/Link/Rect[0 0 50 50]"
                                       f"/A<</S/{action}/F(https://example.org/x)>>>>")

    assert "REMOTE_ACTION" in features(scan_bytes(build_pdf(modify)))


def test_xfa_and_rich_media_are_rejected():
    def xfa(document, page):
        stream = new_object(document, "<<>>")
        document.update_stream(stream, b"<xdp:xdp/>")
        document.xref_set_key(document.pdf_catalog(), "AcroForm", f"<</Fields[]/XFA {stream} 0 R>>")

    def rich_media(document, page):
        add_annotation(document, page, "<</Type/Annot/Subtype/RichMedia/Rect[0 0 50 50]"
                                       "/RichMediaContent<<>>>>")

    assert "XFA_FORM" in features(scan_bytes(build_pdf(xfa)))
    assert "RICH_MEDIA" in features(scan_bytes(build_pdf(rich_media)))


# ============================================================
# REJECT: embedded files
# ============================================================

def test_embedded_executable_is_rejected():
    def modify(document, page):
        document.embfile_add("payload.exe", b"MZ\x90\x00synthetic")

    assert "EMBEDDED_EXECUTABLE" in features(scan_bytes(build_pdf(modify)))


def test_other_embedded_file_is_rejected():
    def modify(document, page):
        document.embfile_add("notes.txt", b"synthetic attachment")

    assert "EMBEDDED_FILE" in features(scan_bytes(build_pdf(modify)))


@pytest.mark.parametrize(
    "payload, relationship, mime",
    [
        (b"MZ\x90\x00 not a manifest", "C2PA_Manifest", "application/c2pa"),
        (jumbf_manifest(b"MZ\x90\x00This program cannot be run in DOS mode"), "C2PA_Manifest", "application/c2pa"),
        (jumbf_manifest(), "Data", "application/c2pa"),
        (jumbf_manifest(), "C2PA_Manifest", "application/octet-stream"),
    ],
)
def test_fake_c2pa_manifests_are_rejected(payload, relationship, mime):
    result = scan_bytes(build_pdf(lambda doc, page: attach(doc, payload, relationship=relationship, mime=mime)))

    assert not result.safe
    assert features(result) & {"EMBEDDED_FILE", "EMBEDDED_EXECUTABLE"}
    assert not result.allowed


def test_executable_named_c2pa_attachment_is_rejected():
    result = scan_bytes(build_pdf(lambda doc, page: attach(doc, jumbf_manifest(), name="manifest.exe")))

    assert "EMBEDDED_EXECUTABLE" in features(result)


# ============================================================
# FAIL CLOSED: malformed input
# ============================================================

@pytest.mark.parametrize(
    "content",
    [
        b"%PDF-1.4 broken",
        b"%PDF-1.7\n" + bytes(range(256)) * 20,
        make_pdf("hello")[:40],
        b"",
    ],
)
def test_malformed_pdf_fails_closed(content):
    result = scan_bytes(content)

    assert not result.safe
    assert result.primary.feature == "UNPARSEABLE"


def test_excessive_nesting_fails_closed():
    nested = "[" * 200 + "]" * 200

    result = scan_bytes(build_pdf(lambda doc, page: doc.xref_set_key(doc.pdf_catalog(), "Deep", nested)))

    assert not result.safe
    assert result.primary.feature == "UNPARSEABLE"


def test_scan_result_contains_no_document_content():
    def modify(document, page):
        document.embfile_add("Synthetic_Patient_Name_HIV.exe", b"MZ synthetic")
        document.xref_set_key(document.pdf_catalog(), "OpenAction",
                              "<</S/JavaScript/JS(var secret='SYNTHETIC-PHI')>>")

    serialized = str(scan_bytes(build_pdf(modify)).to_dict())

    assert "Synthetic_Patient_Name" not in serialized
    assert "SYNTHETIC-PHI" not in serialized
    assert "SYNTHETIC TEST REPORT" not in serialized


# ============================================================
# PIPELINE (isolated worker, sanitisation, audit, quarantine)
# ============================================================

def stored_bytes(ref: str) -> bytes:
    db = SessionLocal()
    try:
        document = db.query(Document).filter(Document.ref == ref).one()
        return vault.load(db, document)
    finally:
        db.close()


def test_pipeline_accepts_reported_pdf_and_strips_manifest(patient_ref):
    result = ingest(patient_ref, FIXTURE.read_bytes())

    assert result["ingestion_status"] == "PROCESSED", result
    assert result["security_scan"]["safe"] is True
    assert result["security_scan"]["allowed"][0]["feature"] == "C2PA_MANIFEST"
    assert any(stage["stage"] == "ACTIVE_CONTENT_SCAN" and stage["status"] == "SAFE"
               for stage in result["stages"])

    stored = stored_bytes(result["ref"])
    assert b"EmbeddedFile" not in stored
    assert b"jumb" not in stored

    reopened = pymupdf.open(stream=stored, filetype="pdf")
    try:
        assert reopened.embfile_count() == 0
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "catalog_key, value, code, feature",
    [
        ("OpenAction", "<</S/JavaScript/JS(app.alert(1))>>", "SUSPICIOUS_PDF", "JAVASCRIPT"),
        ("OpenAction", "<</S/Launch/F(cmd.exe)>>", "SUSPICIOUS_PDF", "LAUNCH_ACTION"),
    ],
)
def test_pipeline_rejects_active_pdf_with_structured_result(patient_ref, catalog_key, value, code, feature, monkeypatch):
    from app.security import scanner

    # Exercise the structural (worker) layer on its own: the byte-level
    # scan that runs first would otherwise catch these too.
    monkeypatch.setattr(scanner, "scan", lambda *args: scanner.ScanResult(True, "test"))

    content = build_pdf(lambda doc, page: doc.xref_set_key(doc.pdf_catalog(), catalog_key, value))

    with pytest.raises(FileValidationError) as error:
        ingest(patient_ref, content)

    assert error.value.code == code
    assert error.value.security_scan["safe"] is False
    assert error.value.security_scan["detected_feature"] == feature
    assert error.value.message == "File could not be accepted."

    db = SessionLocal()
    try:
        assert db.query(Document).count() == 0
        reasons = [event.reason for event in db.query(AuditEvent).filter(AuditEvent.action == "DOCUMENT_REJECTED")]
        assert any(reason.startswith(f"{code}:{feature}:") for reason in reasons)
    finally:
        db.close()


def test_pipeline_rejects_corrupted_pdf(patient_ref):
    with pytest.raises(FileValidationError) as error:
        ingest(patient_ref, make_pdf("hello")[:200])

    assert error.value.code == "CORRUPTED_FILE"


def test_portal_upload_endpoint_accepts_reported_pdf(client):
    sent = send_demo(client, None)
    verify_portal(client, sent)
    consent_via_portal(client)

    upload = client.post(
        "/api/v1/portal/documents",
        files={"file": ("JeevaFlow_Clean_Test_Medical_Report.pdf", FIXTURE.read_bytes(), "application/pdf")},
    )

    assert upload.status_code == 200, upload.text
    body = upload.json()
    assert body["ingestion_status"] == "PROCESSED"
    assert body["security_scan"]["verdict"] == "ACCEPT"


def test_portal_upload_endpoint_rejects_javascript_pdf(client):
    sent = send_demo(client, None)
    verify_portal(client, sent)
    consent_via_portal(client)

    content = build_pdf(lambda doc, page: doc.xref_set_key(
        doc.pdf_catalog(), "OpenAction", "<</S/JavaScript/JS(app.alert(1))>>"))

    upload = client.post(
        "/api/v1/portal/documents",
        files={"file": ("synthetic.pdf", content, "application/pdf")},
    )

    assert upload.status_code == 400
    assert upload.json() == {"detail": "File could not be accepted."}
