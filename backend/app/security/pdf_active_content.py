"""
Structural PDF active-content detection.

Runs ONLY inside the isolated processing worker (app/worker.py): it
parses untrusted bytes, so it must never be imported by the API
process, and it must not import app.config.

The document is opened with MuPDF (the same parser that later
renders and rebuilds it) and every object is walked as a real
dictionary/array tree. Decisions are made on parsed keys and name
values, never on substrings of the file or of serialised objects,
so ordinary content (fonts, images, metadata, compressed streams,
text that happens to mention "JS") does not trigger a rejection,
and escaped names such as /J#61vaScript cannot slip past.

Rejected (unsafe):
    JAVASCRIPT          /JS or /JavaScript entries, /S /JavaScript actions,
                        document-level JavaScript name tree
    OPEN_ACTION         /OpenAction that is an action other than GoTo
    ADDITIONAL_ACTIONS  non-empty /AA (event-triggered actions)
    LAUNCH_ACTION       /S /Launch
    REMOTE_ACTION       /SubmitForm, /ImportData, /GoToR, /GoToE
    RICH_MEDIA          RichMedia / 3D / Screen / Movie / Sound content
                        and the matching actions
    XFA_FORM            dynamic XFA forms
    EMBEDDED_EXECUTABLE embedded file with an executable name or payload
    EMBEDDED_FILE       any other embedded / attached file

Allowed and noted:
    C2PA_MANIFEST       a C2PA "Content Credentials" provenance manifest
                        attached as an associated file. It is inert
                        signed metadata (JUMBF), added by many PDF
                        generators. It is validated (relationship, MIME
                        type, JUMBF header, no executable payload) and
                        then removed by sanitisation like all other
                        attachments.

Fail closed: if the file cannot be opened, any object cannot be
loaded, or the structure exceeds the walk limits, the result is
unsafe with feature UNPARSEABLE.

The result never contains document text, string values, or
attachment names (which can contain patient details): only fixed
feature codes, fixed reasons, object types, object numbers and key
paths.
"""

import re
import zlib
from dataclasses import asdict, dataclass, field
from typing import Optional


MAX_OBJECTS = 250_000
MAX_DEPTH = 48
MAX_NODES = 2_000_000
MAX_MANIFEST_BYTES = 8 * 1024 * 1024

# Action types (/S) that execute code, launch programs, contact a
# remote party, or open other files.
DANGEROUS_ACTIONS = {
    "JavaScript": ("JAVASCRIPT", "JavaScript action"),
    "Launch": ("LAUNCH_ACTION", "Launch action can start an external program or open a file"),
    "SubmitForm": ("REMOTE_ACTION", "SubmitForm action sends data to a remote address"),
    "ImportData": ("REMOTE_ACTION", "ImportData action loads data from an external file"),
    "GoToR": ("REMOTE_ACTION", "GoToR action opens another (possibly remote) document"),
    "GoToE": ("REMOTE_ACTION", "GoToE action opens an embedded document"),
    "RichMediaExecute": ("RICH_MEDIA", "RichMediaExecute action runs rich-media commands"),
    "Rendition": ("RICH_MEDIA", "Rendition action plays embedded media"),
    "Movie": ("RICH_MEDIA", "Movie action plays embedded media"),
    "Sound": ("RICH_MEDIA", "Sound action plays embedded media"),
}

# Most severe first; decides which finding is reported as primary.
SEVERITY = [
    "UNPARSEABLE", "ENCRYPTED", "EMBEDDED_EXECUTABLE", "JAVASCRIPT", "LAUNCH_ACTION",
    "REMOTE_ACTION", "RICH_MEDIA", "XFA_FORM", "EMBEDDED_FILE", "OPEN_ACTION",
    "ADDITIONAL_ACTIONS",
]

DYNAMIC_ANNOTATIONS = {"RichMedia", "3D", "Screen", "Movie", "Sound"}

EXECUTABLE_EXTENSIONS = {
    "exe", "dll", "com", "scr", "msi", "msp", "bat", "cmd", "ps1", "psm1",
    "vbs", "vbe", "js", "jse", "wsf", "wsh", "hta", "jar", "sh", "bash",
    "app", "dmg", "pkg", "lnk", "reg", "cpl", "pif", "py", "pl", "rb",
    "docm", "xlsm", "pptm", "elf", "so", "dylib", "apk", "iso",
}

_EXECUTABLE_MAGIC = [
    re.compile(rb"\AMZ"),
    re.compile(rb"This program cannot be run in DOS mode"),
    re.compile(rb"\x7fELF[\x01\x02][\x01\x02]\x01"),
    re.compile(rb"(?:\xcf\xfa\xed\xfe|\xce\xfa\xed\xfe|\xca\xfe\xba\xbe)"),
    re.compile(rb"PK\x03\x04[\x0a\x14\x2d]\x00"),
    re.compile(rb"\A#!"),
]


@dataclass(frozen=True)
class Finding:
    feature: str
    reason: str
    object_type: str
    xref: Optional[int] = None
    path: Optional[str] = None


@dataclass
class PdfScanResult:
    safe: bool
    findings: list = field(default_factory=list)
    allowed: list = field(default_factory=list)
    objects_scanned: int = 0
    repaired: bool = False

    @property
    def primary(self) -> Optional[Finding]:
        return self.findings[0] if self.findings else None

    def to_dict(self) -> dict:
        return {
            "safe": self.safe,
            "verdict": "ACCEPT" if self.safe else "REJECT",
            "detected_feature": self.primary.feature if self.primary else None,
            "reason": self.primary.reason if self.primary else "No active content found",
            "findings": [asdict(item) for item in self.findings],
            "allowed": [asdict(item) for item in self.allowed],
            "objects_scanned": self.objects_scanned,
            "repaired": self.repaired,
        }


class _Unparseable(Exception):
    pass


# ============================================================
# OBJECT ACCESS
# ============================================================

class _Walker:
    def __init__(self, document):
        import pymupdf

        self.m = pymupdf.mupdf
        self.document = document
        self.pdf = pymupdf._as_pdf_document(document)
        self.findings: list[Finding] = []
        self.allowed: list[Finding] = []
        self.nodes = 0
        # Embedded-file stream xrefs approved through a validated
        # C2PA filespec.
        self.approved_streams: set[int] = set()
        self.embedded_streams: set[int] = set()

    # ----- helpers -------------------------------------------------

    def load(self, xref: int):
        try:
            return self.m.pdf_load_object(self.pdf, xref)
        except Exception:
            raise _Unparseable(xref) from None

    def resolve(self, obj):
        try:
            return self.m.pdf_resolve_indirect(obj)
        except Exception:
            raise _Unparseable(None) from None

    def name(self, obj) -> Optional[str]:
        if obj is None:
            return None
        obj = self.resolve(obj)
        return self.m.pdf_to_name(obj) if self.m.pdf_is_name(obj) else None

    def text(self, obj) -> Optional[str]:
        if obj is None:
            return None
        obj = self.resolve(obj)
        if self.m.pdf_is_string(obj):
            return self.m.pdf_to_text_string(obj)
        if self.m.pdf_is_name(obj):
            return self.m.pdf_to_name(obj)
        return None

    def get(self, obj, key: str):
        if obj is None:
            return None
        obj = self.resolve(obj)
        if not self.m.pdf_is_dict(obj):
            return None
        value = self.m.pdf_dict_gets(obj, key)
        return None if self.m.pdf_is_null(value) else value

    def xref_of(self, obj) -> Optional[int]:
        return self.m.pdf_to_num(obj) if self.m.pdf_is_indirect(obj) else None

    def items(self, obj):
        for index in range(self.m.pdf_dict_len(obj)):
            yield (
                self.m.pdf_to_name(self.m.pdf_dict_get_key(obj, index)),
                self.m.pdf_dict_get_val(obj, index),
            )

    def flag(self, feature, reason, object_type, xref, path):
        finding = Finding(feature, reason, object_type, xref, path)
        if finding not in self.findings:
            self.findings.append(finding)

    # ----- walk ----------------------------------------------------

    def walk_object(self, xref: int):
        obj = self.load(xref)
        self.visit(obj, xref, "", 0)

    def visit(self, obj, xref: int, path: str, depth: int):
        self.nodes += 1

        if self.nodes > MAX_NODES or depth > MAX_DEPTH:
            raise _Unparseable(xref)

        # Indirect references are visited as their own objects.
        if self.m.pdf_is_indirect(obj):
            return

        if self.m.pdf_is_array(obj):
            for index in range(self.m.pdf_array_len(obj)):
                self.visit(self.m.pdf_array_get(obj, index), xref, f"{path}[{index}]", depth + 1)
            return

        if not self.m.pdf_is_dict(obj):
            return

        self.inspect_dict(obj, xref, path)

        for key, value in self.items(obj):
            self.visit(value, xref, f"{path}/{key}", depth + 1)

    # ----- rules ---------------------------------------------------

    def inspect_dict(self, obj, xref: int, path: str):
        object_type = self.name(self.get(obj, "Type")) or "Dictionary"
        subtype = self.name(self.get(obj, "Subtype"))

        keys = {key for key, _ in self.items(obj)}

        if "JS" in keys:
            self.flag("JAVASCRIPT", "JavaScript code attached to an action", "Action", xref, f"{path}/JS")

        if "JavaScript" in keys:
            self.flag("JAVASCRIPT", "Document-level JavaScript name tree", object_type, xref, f"{path}/JavaScript")

        action = self.name(self.get(obj, "S"))

        if action in DANGEROUS_ACTIONS and object_type != "StructElem":
            feature, reason = DANGEROUS_ACTIONS[action]
            self.flag(feature, reason, "Action", xref, f"{path}/S")

        if "AA" in keys:
            additional = self.resolve(self.get(obj, "AA"))
            if self.m.pdf_is_dict(additional) and self.m.pdf_dict_len(additional) > 0:
                self.flag(
                    "ADDITIONAL_ACTIONS",
                    "Additional actions (/AA) run automatically on document, page or field events",
                    object_type, xref, f"{path}/AA",
                )

        if "OpenAction" in keys:
            self.inspect_open_action(self.get(obj, "OpenAction"), xref, path)

        if "XFA" in keys:
            self.flag("XFA_FORM", "Dynamic XFA form (scriptable)", "AcroForm", xref, f"{path}/XFA")

        is_annotation = object_type == "Annot" or "Rect" in keys

        if (is_annotation and subtype in DYNAMIC_ANNOTATIONS) or "RichMediaContent" in keys:
            self.flag(
                "RICH_MEDIA", f"{subtype or 'RichMedia'} content can run embedded media or scripts",
                "Annot", xref, f"{path}/Subtype",
            )

        if object_type == "EmbeddedFile":
            self.embedded_streams.add(xref)

        if "EF" in keys:
            self.inspect_filespec(obj, xref, path)

    def inspect_open_action(self, value, xref: int, path: str):
        target = self.resolve(value)

        # A destination array ([page /Fit]) only sets the initial view.
        if self.m.pdf_is_array(target) or self.m.pdf_is_null(target):
            return

        if self.m.pdf_is_dict(target) and self.name(self.get(target, "S")) == "GoTo":
            return

        self.flag(
            "OPEN_ACTION", "OpenAction runs an action other than navigation when the document opens",
            "Catalog", xref, f"{path}/OpenAction",
        )

    # ----- embedded files -------------------------------------------

    def inspect_filespec(self, filespec, xref: int, path: str):
        ef = self.resolve(self.get(filespec, "EF"))
        streams = []

        if self.m.pdf_is_dict(ef):
            for _, value in self.items(ef):
                stream_xref = self.xref_of(value)
                if stream_xref is not None:
                    streams.append(stream_xref)

        names = [self.text(self.get(filespec, key)) or "" for key in ("F", "UF")]

        if any(_executable_name(name) for name in names):
            self.flag(
                "EMBEDDED_EXECUTABLE", "Embedded file has an executable or script file type",
                "Filespec", xref, f"{path}/EF",
            )
            return

        if self.is_c2pa_manifest(filespec, streams):
            self.approved_streams.update(streams)
            self.allowed.append(Finding(
                "C2PA_MANIFEST",
                "C2PA Content Credentials provenance manifest (inert metadata); removed during sanitisation",
                "Filespec", xref, f"{path}/EF",
            ))
            return

        self.flag(
            "EMBEDDED_FILE", "Document carries an embedded or attached file",
            "Filespec", xref, f"{path}/EF",
        )

    def is_c2pa_manifest(self, filespec, streams: list) -> bool:
        relationship = self.name(self.get(filespec, "AFRelationship"))
        mime = self.text(self.get(filespec, "Subtype")) or ""

        if relationship != "C2PA_Manifest" or mime.lower() != "application/c2pa" or len(streams) != 1:
            return False

        payload = self.stream_payload(streams[0])

        if payload is None or len(payload) < 16:
            return False

        # JUMBF superbox: [length][jumb] followed by its [length][jumd]
        # description box.
        declared = int.from_bytes(payload[:4], "big")

        if payload[4:8] != b"jumb" or payload[12:16] != b"jumd" or declared > len(payload) or declared < 16:
            return False

        if b"c2pa" not in payload[:64]:
            return False

        if any(pattern.search(payload) for pattern in _EXECUTABLE_MAGIC):
            self.flag(
                "EMBEDDED_EXECUTABLE", "Embedded manifest contains an executable payload",
                "EmbeddedFile", streams[0], None,
            )
            return False

        return True

    def stream_payload(self, xref: int) -> Optional[bytes]:
        """
        Bounded decode of an embedded-file stream. Only unfiltered or
        Flate streams are accepted for a manifest.
        """

        stream = self.load(xref)

        if not self.document.xref_is_stream(xref):
            return None

        filters = self.resolve(self.m.pdf_dict_gets(stream, "Filter"))

        if self.m.pdf_is_array(filters):
            if self.m.pdf_array_len(filters) > 1:
                return None
            filters = self.resolve(self.m.pdf_array_get(filters, 0)) if self.m.pdf_array_len(filters) else filters

        filter_name = self.m.pdf_to_name(filters) if self.m.pdf_is_name(filters) else None

        try:
            raw = self.document.xref_stream_raw(xref)
        except Exception:
            raise _Unparseable(xref) from None

        if raw is None or len(raw) > MAX_MANIFEST_BYTES:
            return None

        if filter_name is None:
            return raw

        if filter_name != "FlateDecode":
            return None

        decoder = zlib.decompressobj()

        try:
            data = decoder.decompress(raw, MAX_MANIFEST_BYTES)
        except zlib.error:
            return None

        return None if decoder.unconsumed_tail else data

    def finish(self):
        for xref in sorted(self.embedded_streams - self.approved_streams):
            self.flag(
                "EMBEDDED_FILE", "Embedded file stream not covered by an accepted attachment",
                "EmbeddedFile", xref, None,
            )


def _executable_name(name: str) -> bool:
    if "." not in name:
        return False
    return name.rsplit(".", 1)[-1].strip().lower() in EXECUTABLE_EXTENSIONS


# ============================================================
# ENTRY POINTS
# ============================================================

def scan_document(document) -> PdfScanResult:
    """
    Scan an already-opened pymupdf.Document. Never raises for
    content problems: an unparseable structure is an unsafe result.
    """

    walker = _Walker(document)
    repaired = bool(getattr(document, "is_repaired", False))

    try:
        count = document.xref_length()

        if count > MAX_OBJECTS:
            raise _Unparseable(None)

        for xref in range(1, count):
            walker.walk_object(xref)

        walker.finish()

    except _Unparseable as exc:
        xref = exc.args[0] if exc.args else None
        return PdfScanResult(
            False,
            [Finding("UNPARSEABLE", "PDF structure could not be parsed safely", "Object", xref, None)],
            objects_scanned=walker.nodes,
            repaired=repaired,
        )

    findings = sorted(walker.findings, key=lambda item: SEVERITY.index(item.feature))

    return PdfScanResult(
        not findings,
        findings,
        walker.allowed,
        objects_scanned=document.xref_length() - 1,
        repaired=repaired,
    )


def scan_bytes(data: bytes) -> PdfScanResult:
    import pymupdf

    try:
        document = pymupdf.open(stream=data, filetype="pdf")
    except Exception:
        return PdfScanResult(False, [Finding("UNPARSEABLE", "PDF could not be opened", "Document")])

    try:
        if document.needs_pass or document.is_encrypted:
            return PdfScanResult(False, [Finding("ENCRYPTED", "Encrypted PDFs cannot be inspected", "Document")])

        return scan_document(document)
    finally:
        document.close()
