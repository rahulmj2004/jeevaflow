"""
Generates the report's SVG diagrams into ../assets/diagrams/.

Every diagram reflects behaviour verified in the repository
(module and endpoint names are the real ones). Run:

    python3 docs/report/build/diagrams.py
"""

from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).resolve().parent.parent / "assets" / "diagrams"

FONT = "'Helvetica Neue', Helvetica, Arial, sans-serif"
MONO = "Menlo, 'SF Mono', Consolas, monospace"

INK = "#0F172A"
MUTED = "#475569"
LINE = "#94A3B8"
TEAL = "#0F766E"
TEAL_L = "#E6F4F1"
BLUE = "#1D4ED8"
BLUE_L = "#EAF1FE"
SLATE_L = "#F1F5F9"
AMBER = "#B45309"
AMBER_L = "#FEF3C7"
RED = "#B91C1C"
RED_L = "#FDECEC"
PURPLE = "#6D28D9"
PURPLE_L = "#F1EBFE"
GREEN = "#15803D"
GREEN_L = "#E8F6EC"

PALETTE = {
    "teal": (TEAL_L, TEAL),
    "blue": (BLUE_L, BLUE),
    "slate": (SLATE_L, MUTED),
    "amber": (AMBER_L, AMBER),
    "red": (RED_L, RED),
    "purple": (PURPLE_L, PURPLE),
    "green": (GREEN_L, GREEN),
    "white": ("#FFFFFF", LINE),
}


def svg(width, height, body, title):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img" aria-label="{escape(title)}" '
        f'font-family="{FONT}">'
        "<defs>"
        f'<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M0,0 L10,5 L0,10 z" fill="{MUTED}"/></marker>'
        f'<marker id="arrow-red" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M0,0 L10,5 L0,10 z" fill="{RED}"/></marker>'
        f'<marker id="arrow-teal" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M0,0 L10,5 L0,10 z" fill="{TEAL}"/></marker>'
        "</defs>"
        f'<rect width="{width}" height="{height}" fill="#FFFFFF"/>'
        f"{body}</svg>"
    )


def text(x, y, value, size=13, weight=400, color=INK, anchor="start", mono=False, italic=False):
    family = f' font-family="{MONO}"' if mono else ""
    style = ' font-style="italic"' if italic else ""
    return (
        f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" fill="{color}" '
        f'text-anchor="{anchor}"{family}{style}>{escape(value)}</text>'
    )


def box(x, y, w, h, title, sub=None, tone="white", size=13.5, sub_size=11.5, radius=8, align="center", mono_sub=False, dashed=False):
    fill, stroke = PALETTE[tone]
    dash = ' stroke-dasharray="5 4"' if dashed else ""
    parts = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{fill}" stroke="{stroke}" stroke-width="1.4"{dash}/>']
    lines = [title] if isinstance(title, str) else title
    subs = [] if sub is None else ([sub] if isinstance(sub, str) else sub)
    total = len(lines) * (size + 3) + len(subs) * (sub_size + 3)
    cy = y + (h - total) / 2 + size
    ax = x + w / 2 if align == "center" else x + 12
    anchor = "middle" if align == "center" else "start"
    title_color = stroke if tone not in ("white", "slate") else INK
    for line in lines:
        parts.append(text(ax, cy, line, size, 600, title_color, anchor))
        cy += size + 3
    for line in subs:
        parts.append(text(ax, cy, line, sub_size, 400, MUTED, anchor, mono=mono_sub))
        cy += sub_size + 3
    return "".join(parts)


def arrow(x1, y1, x2, y2, color=MUTED, dashed=False, width=1.5, marker="arrow"):
    dash = ' stroke-dasharray="5 4"' if dashed else ""
    return f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="{width}"{dash} marker-end="url(#{marker})"/>'


def path(d, color=MUTED, dashed=False, width=1.5, marker="arrow"):
    dash = ' stroke-dasharray="5 4"' if dashed else ""
    end = f' marker-end="url(#{marker})"' if marker else ""
    return f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{width}"{dash}{end}/>'


def band(x, y, w, h, label, tone="slate", align="left"):
    fill, stroke = PALETTE[tone]
    lx, anchor = (x + 14, "start") if align == "left" else (x + w - 14, "end")
    return (
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="{fill}" fill-opacity="0.55" stroke="{stroke}" stroke-opacity="0.45" stroke-width="1"/>'
        + text(lx, y + 20, label.upper(), 11, 700, stroke, anchor)
    )


def pill(x, y, label, tone="teal", size=11):
    fill, stroke = PALETTE[tone]
    w = len(label) * size * 0.62 + 16
    return (
        f'<rect x="{x}" y="{y}" width="{w}" height="{size + 9}" rx="{(size + 9) / 2}" fill="{fill}" stroke="{stroke}" stroke-width="1"/>'
        + text(x + w / 2, y + size + 3, label, size, 700, stroke, "middle")
    )


def save(name, content):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.svg").write_text(content, encoding="utf-8")
    print("wrote", name)


# ============================================================
# 1. SYSTEM ARCHITECTURE
# ============================================================

def architecture():
    W, H = 980, 1010
    b = []
    # Zones
    zones = [
        (40, "1 · Untrusted communication channel", "amber", 120),
        (175, "2 · Patient verification and consent", "blue", 120),
        (310, "3 · Secure ingestion (fail closed)", "teal", 120),
        (445, "4 · Clinical organisation (no diagnosis)", "purple", 205),
        (665, "5 · Clinician access", "blue", 120),
        (800, "6 · Accountability", "slate", 95),
    ]
    for y, label, tone, h in zones:
        b.append(band(20, y, 720, h, label, tone, "left" if y in (40, 800) else "right"))

    def row(y, items, x0=36, w=158, gap=22, h=66):
        xs = []
        for i, (title, sub, tone) in enumerate(items):
            x = x0 + i * (w + gap)
            b.append(box(x, y, w, h, title, sub, tone, size=12.5, sub_size=10.5))
            xs.append(x)
            if i:
                b.append(arrow(x - gap + 1, y + h / 2, x - 3, y + h / 2))
        return xs

    r1 = row(70, [
        ("Patient", ["sends a document", "or texts “Hi”"], "white"),
        ("WhatsApp", ["via Twilio", "(transport only)"], "amber"),
        ("Webhook", ["POST /api/v1/", "webhooks/whatsapp"], "white"),
        ("Signature check", ["X-Twilio-Signature,", "replay, account, age"], "amber"),
    ])
    r2 = row(205, [
        ("Transaction + OTP", ["txn_ ref, 6-digit code,", "5 min, single use"], "blue"),
        ("Portal session", ["POST /portal/verify", "15-min cookie + CSRF"], "white"),
        ("Consent engine", ["doctor · purpose ·", "scopes · expiry"], "blue"),
        ("Quarantine release", ["documents read only", "after OTP + consent"], "white"),
    ])
    r3 = row(340, [
        ("Validation", ["size, type,", "magic bytes"], "white"),
        ("Isolated worker", ["structure, active content,", "sanitise / rebuild"], "teal"),
        ("Malware scan", ["ClamAV or heuristic", "(original + sanitised)"], "white"),
        ("Encrypted vault", ["AES-256-GCM, per-doc", "DEK · QUARANTINED"], "teal"),
    ])
    r4a = row(475, [
        ("OCR / extraction", ["PyMuPDF text or local", "Tesseract, rule-based"], "purple"),
        ("Structured facts", ["labs, instructions,", "medications, allergies"], "white"),
        ("Source provenance", ["quote-or-reject:", "page, span, bbox"], "purple"),
    ])
    r4b = row(565, [
        ("Patient timeline", ["dated by report date", "(fallback: received)"], "white"),
        ("Conflict detection", ["same test + same date,", "different values"], "purple"),
        ("Care loops", ["instructions tracked;", "humans close them"], "white"),
    ])
    r5 = row(695, [
        ("Doctor sign-in", ["POST /auth/login", "(scrypt password)"], "white"),
        ("TOTP MFA", ["POST /auth/mfa", "replay blocked"], "blue"),
        ("Consent authorization", ["authorize_doctor()", "on every request"], "blue"),
        ("Doctor-ready form", ["+ evidence viewer", "(watermarked, 60 s token)"], "white"),
    ])
    b.append(box(36, 830, 698, 52, "Tamper-evident audit trail", "SHA-256 hash chain + HMAC · head anchored in identity DB · verified by auditor (POST /api/v1/audit/verify)", "slate", size=13, sub_size=10.5))

    # row-to-row connectors (end of row -> start of next row)
    def link(x_from, y_from, x_to, y_to):
        mid = (y_from + y_to) / 2
        b.append(path(f"M{x_from},{y_from} L{x_from},{mid} L{x_to},{mid} L{x_to},{y_to - 3}"))

    link(r1[3] + 79, 136, r2[0] + 79, 205)
    link(r2[3] + 79, 271, r3[0] + 79, 340)
    link(r3[3] + 79, 406, r4a[0] + 79, 475)
    b.append(arrow(r4a[2] + 79, 541, r4a[2] + 79, 562))
    b.append(path(f"M{r4a[2] + 79},541 L{r4a[2] + 79},553 L{r4b[0] + 79},553 L{r4b[0] + 79},562", marker="arrow"))
    link(r4b[2] + 79, 631, r5[0] + 79, 695)
    b.append(arrow(r5[3] + 79, 761, r5[3] + 79, 827))

    # Roles column
    b.append(text(770, 52, "AUTHORIZED ROLES", 11, 700, MUTED))
    roles = [
        (70, "PATIENT", "OTP session (no password)", ["own consents and", "own documents only"], "white"),
        (215, "DOCTOR", "password + TOTP", ["consented patients only;", "scope-limited reads;", "verify / reject, loops"], "blue"),
        (395, "AUDITOR", "password + TOTP", ["security status,", "audit events + verify;", "no clinical data"], "slate"),
        (560, "ADMIN", "password + TOTP", ["enrol patients, WhatsApp", "status, retention run,", "audit; no clinical data"], "slate"),
    ]
    for y, title, auth, subs, tone in roles:
        b.append(box(760, y, 200, 112, [title], [auth, ""] + subs, tone, size=13.5, sub_size=10.5))
    b.append(text(860, 700, "Every role is deny-by-default:", 10.5, 400, MUTED, "middle"))
    b.append(text(860, 714, "require_roles() + server-side", 10.5, 400, MUTED, "middle"))
    b.append(text(860, 728, "sessions (HttpOnly, SameSite=Strict).", 10.5, 400, MUTED, "middle"))

    b.append(text(20, 925, "WhatsApp transports the interaction; JeevaFlow controls the healthcare data. Nothing clinical is ever sent back over WhatsApp.", 12, 600, TEAL))
    b.append(text(20, 945, "Modules: routes/webhook.py · whatsapp.py · security/otp.py · security/consent.py · pipeline.py · worker.py · vault.py ·", 10.5, 400, MUTED))
    b.append(text(20, 960, "extraction.py · facts.py · provenance.py · timeline.py · conflicts.py · loops.py · matching.py · security/auth.py · routes/doctor.py ·", 10.5, 400, MUTED))
    b.append(text(20, 975, "doctor_brief.py · security/audit.py · retention.py", 10.5, 400, MUTED))
    save("01_architecture", svg(W, H, "".join(b), "JeevaFlow system architecture"))


# ============================================================
# 2. WHATSAPP TRUST BOUNDARY
# ============================================================

def whatsapp_boundary():
    W, H = 900, 400
    b = []
    b.append(band(15, 30, 300, 350, "Outside the boundary (untrusted)", "amber"))
    b.append(band(345, 30, 540, 350, "JeevaFlow controlled environment", "teal"))
    b.append(box(40, 70, 250, 56, "Patient's WhatsApp", "sends PDF / photo / text", "white"))
    b.append(box(40, 160, 250, 56, "Twilio (WhatsApp API)", "signs every webhook (HMAC-SHA1)", "amber"))
    b.append(box(40, 250, 250, 104, "What goes back to WhatsApp", ["generic templates only:", "“securely received…”, OTP,", "“processed — continue to portal”.", "No results, no medicines."], "white", sub_size=11))
    b.append(arrow(165, 126, 165, 157))

    checks = [
        ("Body cap 64 KB + invalid-signature IP limit", "white"),
        ("X-Twilio-Signature (fails closed, 403)", "amber"),
        ("MessageSid replay table + AccountSid check", "white"),
        ("Freshness check (≤ 10 min) + per-sender limit", "white"),
        ("Enrolled number? else generic reply, no download", "white"),
        ("Media fetched server-side, then deleted at Twilio", "white"),
        ("receive_document() → quarantine (not read yet)", "teal"),
    ]
    y = 62
    for label, tone in checks:
        b.append(box(370, y, 330, 36, label, None, tone, size=12, align="left"))
        y += 44
    for i in range(len(checks) - 1):
        b.append(arrow(535, 98 + i * 44, 535, 103 + i * 44 + 2))
    b.append(path("M290,188 L330,188 L330,80 L366,80"))
    b.append(box(720, 150, 150, 140, "Portal link + OTP", ["patient continues", "in the JeevaFlow", "portal, not in chat:", "verify → consent", "→ processing"], "blue", sub_size=11))
    b.append(arrow(700, 350, 790, 293))
    b.append(path("M366,330 L330,330 L330,300 L292,300", color=TEAL, marker="arrow-teal", dashed=True))
    save("02_whatsapp_boundary", svg(W, H, "".join(b), "WhatsApp trust boundary"))


# ============================================================
# 3. OTP SEQUENCE
# ============================================================

def otp_sequence():
    W, H = 900, 560
    lanes = [("Patient", 80), ("WhatsApp / Twilio", 250), ("Webhook", 430), ("Portal (browser)", 610), ("JeevaFlow API", 800)]
    b = []
    for name, x in lanes:
        b.append(box(x - 75, 20, 150, 40, name, None, "teal" if name == "JeevaFlow API" else "white", size=12.5))
        b.append(f'<line x1="{x}" y1="60" x2="{x}" y2="540" stroke="{LINE}" stroke-width="1" stroke-dasharray="4 4"/>')

    steps = [
        (80, 250, "1  document or “Hi”"),
        (250, 430, "2  signed POST"),
        (430, 800, "3  verify signature, replay, sender enrolled"),
        (800, 800, "4  create txn_… + 6-digit OTP (HMAC stored)"),
        (430, 250, "5  generic reply + portal link"),
        (250, 80, "6  link + OTP"),
        (80, 610, "7  open link, enter code"),
        (610, 800, "8  POST /api/v1/portal/verify"),
        (800, 800, "9  ≤5 tries, back-off, single use"),
        (800, 610, "10  HttpOnly session cookie (15 min) + CSRF token"),
        (610, 800, "11  consent, documents (session + CSRF)"),
        (610, 800, "12  POST /api/v1/portal/logout → session revoked"),
    ]
    y = 95
    for x1, x2, label in steps:
        if x1 == x2:
            b.append(path(f"M{x1},{y - 6} l40,0 l0,14 l-37,0", marker="arrow"))
            b.append(text(x1 - 12, y + 3, label, 11.5, 500, INK, "end"))
        else:
            b.append(arrow(x1, y, x2 - (4 if x2 > x1 else -4), y, color=TEAL if x2 == 800 else MUTED, marker="arrow-teal" if x2 == 800 else "arrow"))
            b.append(text((x1 + x2) / 2, y - 6, label, 11.5, 500, INK, "middle"))
        y += 37
    save("03_otp_sequence", svg(W, H, "".join(b), "Patient OTP verification sequence"))


# ============================================================
# 4. CONSENT LIFECYCLE
# ============================================================

def consent_lifecycle():
    W, H = 900, 330
    b = []
    b.append(box(20, 120, 150, 70, "Select doctor", ["portal session:", "doctor, scopes, 1/7/30 d"], "white", sub_size=11))
    b.append(box(220, 120, 150, 70, "GRANT", ["POST /portal/consents", "CONSENT_GRANTED"], "blue", sub_size=10.5))
    b.append(box(420, 120, 150, 70, "ACTIVE", ["checked on every", "doctor request"], "teal", sub_size=11))
    b.append(box(660, 20, 220, 70, "REVOKED", ["POST …/{ref}/revoke", "revoked_at set · CONSENT_REVOKED"], "red", sub_size=10.5))
    b.append(box(660, 120, 220, 70, "ENDED NOW", ["POST …/{ref}/expire", "expires_at = now"], "amber", sub_size=10.5))
    b.append(box(660, 220, 220, 70, "EXPIRED", ["time passes expires_at;", "retention job audits once"], "slate", sub_size=10.5))
    b.append(arrow(170, 155, 217, 155))
    b.append(arrow(370, 155, 417, 155))
    b.append(path("M570,140 L615,140 L615,55 L657,55"))
    b.append(arrow(570, 155, 657, 155))
    b.append(path("M570,170 L615,170 L615,255 L657,255"))
    b.append(text(450, 225, "Any of the three ⇒ authorize_doctor() finds", 11.5, 600, RED, "middle"))
    b.append(text(450, 241, "no active consent ⇒ generic 403 on the next request", 11.5, 600, RED, "middle"))
    b.append(text(20, 312, "There is no doctor-initiated “request” state in the code: consent is patient-initiated from the verified portal. Purpose is fixed to CLINICAL_REVIEW.", 11, 400, MUTED))
    save("04_consent_lifecycle", svg(W, H, "".join(b), "Consent lifecycle"))


# ============================================================
# 5. DOCUMENT LIFECYCLE
# ============================================================

def document_lifecycle():
    W, H = 900, 420
    b = []
    b.append(text(20, 26, "receive_document()  — runs before any OTP / consent", 12, 700, TEAL))
    top = [
        ("Upload / media", ["portal, camera", "or WhatsApp"], "white"),
        ("Validate", ["≤10 MB, PDF/JPG/PNG,", "magic bytes"], "white"),
        ("Worker inspect", ["active content, pages,", "bombs, sanitise"], "teal"),
        ("Scan", ["ClamAV or heuristic,", "original + sanitised"], "white"),
        ("Hash + dedupe", ["SHA-256 per", "patient"], "white"),
        ("Encrypt + store", ["AES-256-GCM DEK,", "status QUARANTINED"], "teal"),
    ]
    x = 20
    for i, (t, s, tone) in enumerate(top):
        b.append(box(x, 40, 132, 70, t, s, tone, size=12.5, sub_size=10.5))
        if i:
            b.append(arrow(x - 13, 75, x - 3, 75))
        x += 146
    b.append(box(20, 140, 860, 44, "Any failure ⇒ FileValidationError: nothing stored, DOCUMENT_REJECTED audited with a code (e.g. SUSPICIOUS_PDF:JAVASCRIPT)", None, "red", size=12))
    b.append(arrow(450, 113, 450, 137, color=RED, marker="arrow-red"))

    b.append(text(20, 222, "process_document()  — only after OTP verification AND an active consent", 12, 700, PURPLE))
    bottom = [
        ("Decrypt in memory", ["vault.load()"], "white"),
        ("Isolated OCR", ["fresh process, no", "secrets, no network"], "purple"),
        ("Rule extraction", ["labs, instructions,", "medications"], "white"),
        ("Quote-or-reject", ["unanchored items", "are dropped"], "purple"),
        ("Store facts", ["status REVIEW;", "PROCESSED"], "white"),
        ("Match loops", ["POTENTIAL_MATCH", "suggestions only"], "white"),
    ]
    x = 20
    for i, (t, s, tone) in enumerate(bottom):
        b.append(box(x, 236, 132, 70, t, s, tone, size=12.5, sub_size=10.5))
        if i:
            b.append(arrow(x - 13, 271, x - 3, 271))
        x += 146
    b.append(path("M816,110 L816,128 L900,128", marker=None, color="#FFFFFF"))
    b.append(box(20, 336, 420, 66, "Retention (retention.py, every 5 min)", ["unverified quarantined docs shredded when txn expires;", "all docs crypto-shredded after 90 days"], "slate", sub_size=10.5))
    b.append(box(460, 336, 420, 66, "Deletion = crypto-shredding (vault.destroy)", ["destroy wrapped DEK → overwrite + unlink object", "→ delete DB rows → DOCUMENT_DELETED audit"], "slate", sub_size=10.5))
    save("05_document_lifecycle", svg(W, H, "".join(b), "Document lifecycle"))


# ============================================================
# 6. PROVENANCE
# ============================================================

def provenance():
    W, H = 900, 300
    b = []
    b.append(box(20, 40, 190, 150, "Source document", ["sha256 of original", "encrypted in vault", "page 1 text:", "“HbA1c: 9.4 %”"], "white", sub_size=11))
    b.append(box(250, 40, 190, 150, "Extracted item", ["observation_type HbA1c", "value 9.4  unit %", "state SOURCE_FACT", "review_status REVIEW"], "purple", sub_size=11, mono_sub=False))
    b.append(box(480, 40, 190, 150, "SourceEvidence", ["quote (exact text)", "page_number", "start / end position", "bbox + confidence", "document sha256 + version"], "teal", sub_size=11))
    b.append(box(710, 40, 170, 150, "Doctor review", ["sees quote + page;", "opens watermarked", "region; Verify /", "Confirm / Reject"], "blue", sub_size=11))
    b.append(arrow(210, 115, 247, 115))
    b.append(arrow(440, 115, 477, 115))
    b.append(arrow(670, 115, 707, 115))
    b.append(box(250, 215, 420, 58, "locate_quote() before storing", "quote not found in page text ⇒ item dropped (evidence_rejected++)", "red", size=12.5, sub_size=11))
    b.append(arrow(460, 190, 460, 212, color=RED, marker="arrow-red"))
    save("06_provenance", svg(W, H, "".join(b), "Source provenance chain"))


# ============================================================
# 7. DOCTOR-READY FORM FLOW
# ============================================================

def doctor_form_flow():
    W, H = 900, 470
    b = []
    docs = [
        ("Synthetic prescription", "20 Sep 2026"),
        ("Synthetic lab report", "15 Jun 2026"),
        ("Follow-up HbA1c", "20 Sep 2026"),
        ("Transcribed copy", "15 Jun 2026"),
    ]
    y = 30
    for title, date in docs:
        b.append(box(20, y, 190, 52, title, date, "white", size=12.5, sub_size=11))
        b.append(path(f"M210,{y + 26} L240,{y + 26} L240,215 L262,215"))
        y += 64
    stages = [
        ("Extraction", "rules + local OCR"),
        ("Normalisation", "“1-0-1” → twice daily (AI_INFERRED label)"),
        ("Facts + states", "SOURCE_FACT / AI_INFERRED / UNCERTAIN / MISSING"),
        ("Timeline", "ordered by report date"),
        ("Conflicts", "same test, same date"),
        ("Care loops", "instructions + potential matches"),
    ]
    y = 22
    for i, (t, s) in enumerate(stages):
        b.append(box(265, y, 300, 52, t, s, "purple" if i % 2 == 0 else "white", size=12.5, sub_size=10.5))
        if i:
            b.append(arrow(415, y - 14, 415, y - 3))
        y += 66
    b.append(arrow(565, 215, 600, 215))
    b.append(box(605, 22, 275, 400, "DOCTOR-READY FORM", [
        "GET /doctor/patients/{ref}/form",
        "",
        "• Patient reference (+ name/DOB",
        "   only with DEMOGRAPHICS scope)",
        "• Consent ref, scopes, expiry",
        "• Medications: dose, frequency,",
        "   duration, each with a state",
        "• Prescriber (from source)",
        "• Laboratory results: latest +",
        "   earlier values, conflict flag",
        "• Allergies",
        "• Source-supported notes (loops)",
        "• Uncertain information",
        "• Missing information",
        "• Source evidence: document,",
        "   report date, SHA-256",
        "• “Not shared” scopes listed",
        "• Notice: no diagnosis / prescribing",
    ], "teal", size=14, sub_size=11.5, align="left"))
    b.append(text(20, 450, "Scopes not covered by the active consent are omitted server-side (doctor_brief.py), not hidden in the UI.", 11.5, 600, TEAL))
    save("07_doctor_form_flow", svg(W, H, "".join(b), "From scattered documents to the doctor-ready form"))


# ============================================================
# 8. CONFLICT
# ============================================================

def conflict():
    W, H = 900, 270
    b = []
    b.append(box(20, 30, 230, 80, "Document A · lab report", ["Report Date 15/06/2026", "HbA1c: 9.4 %"], "white", sub_size=11.5))
    b.append(box(20, 140, 230, 80, "Document B · transcribed copy", ["Report Date 15/06/2026", "HbA1c: 8.2 %"], "white", sub_size=11.5))
    b.append(box(300, 70, 250, 110, "detect_observation_conflicts()", ["group by (test, date)", "> 1 distinct (value, unit)", "rejected observations excluded"], "purple", sub_size=11))
    b.append(path("M250,70 L275,70 L275,125 L297,125"))
    b.append(path("M250,180 L275,180 L275,125"))
    b.append(box(600, 30, 280, 190, "CONFLICT", [
        "status HUMAN_REVIEW_REQUIRED",
        "both values kept, each with",
        "its own evidence + source link",
        "",
        "JeevaFlow does not pick a value.",
        "The doctor verifies or rejects",
        "each observation; a rejected",
        "one leaves the conflict set.",
    ], "red", sub_size=11.5))
    b.append(arrow(550, 125, 597, 125))
    b.append(text(20, 250, "Different dates (9.4 % on 15 Jun, 8.1 % on 20 Sep) form a series, not a conflict.", 11.5, 600, MUTED))
    save("08_conflict", svg(W, H, "".join(b), "Conflict detection"))


# ============================================================
# 9. CARE LOOPS
# ============================================================

def loops():
    W, H = 900, 330
    b = []
    b.append(box(20, 120, 150, 70, "OPEN", ["instruction read", "with evidence"], "blue", sub_size=11))
    b.append(box(230, 20, 170, 70, "OVERDUE", ["computed: OPEN and", "past due date"], "amber", sub_size=11))
    b.append(box(230, 120, 170, 70, "POTENTIAL_MATCH", ["newer source-backed", "result (system)"], "purple", sub_size=11))
    b.append(box(230, 220, 170, 70, "NEEDS_REVIEW", ["doctor: POST …/review"], "slate", sub_size=11))
    b.append(box(480, 120, 170, 70, "CLOSED", ["doctor: POST …/", "confirm-completion"], "green", sub_size=11))
    b.append(arrow(170, 140, 227, 70))
    b.append(arrow(170, 155, 227, 155))
    b.append(arrow(170, 170, 227, 245))
    b.append(arrow(400, 155, 477, 155, color=GREEN))
    b.append(path("M315,190 L315,217", marker="arrow"))
    b.append(path("M230,140 C200,105 190,105 172,128", marker="arrow", dashed=True))
    b.append(text(190, 100, "keep-open", 10.5, 500, MUTED, "middle"))
    b.append(box(690, 40, 190, 230, "Rules", [
        "System may only suggest",
        "(matching.py): instruction",
        "names the test, newer",
        "document, evidence present,",
        "not rejected.",
        "",
        "Only a signed-in, MFA-",
        "verified doctor closes a",
        "loop. Every change is",
        "written to loop_events and",
        "the audit chain.",
    ], "white", size=13, sub_size=11, align="left"))
    b.append(text(20, 318, "Due dates are derived from the stated interval (“after 3 months”) from the report date; “Follow-up with physician” has no interval, so no due date.", 11, 400, MUTED))
    save("09_loops", svg(W, H, "".join(b), "Care loop states"))


# ============================================================
# 10. MFA
# ============================================================

def mfa():
    W, H = 900, 250
    b = []
    b.append(box(20, 40, 200, 90, "Step 1 · password", ["POST /api/v1/auth/login", "scrypt (N=2¹⁴, r=8, p=1)", "dummy hash for unknown users"], "white", sub_size=10.5))
    b.append(box(260, 40, 200, 90, "Pending-MFA cookie", ["5-minute, not yet a", "session (no data access)"], "slate", sub_size=10.5))
    b.append(box(500, 40, 200, 90, "Step 2 · TOTP", ["POST /api/v1/auth/mfa", "RFC 6238, 30 s, ±1 step", "used step cannot be replayed"], "blue", sub_size=10.5))
    b.append(box(740, 40, 140, 90, "Staff session", ["mfa_verified", "2 h max,", "20 min idle"], "teal", sub_size=10.5))
    b.append(arrow(220, 85, 257, 85))
    b.append(arrow(460, 85, 497, 85))
    b.append(arrow(700, 85, 737, 85))
    b.append(box(20, 160, 860, 60, "Failures", "5 failed logins ⇒ 15-minute lockout · generic “Authentication required.” · LOGIN_FAILED / MFA_FAILED audited · cookies HttpOnly, SameSite=Strict, path=/api", "red", size=12.5, sub_size=11))
    save("10_mfa", svg(W, H, "".join(b), "Staff password plus TOTP"))


# ============================================================
# 11. AUTHORIZATION DECISION
# ============================================================

def authorization():
    W, H = 900, 424
    b = []
    checks = [
        ("Staff session valid + MFA verified", "current_staff(): else 401"),
        ("Role is DOCTOR", "require_doctor: else 403"),
        ("CSRF token on state changes", "X-CSRF-Token: else 403"),
        ("Per-doctor rate limit", "600 requests / 10 min"),
        ("Active consent: this patient → this doctor", "not revoked, not expired, CLINICAL_REVIEW"),
        ("Consent covers the scope read", "LABS, MEDICATIONS, SOURCE_DOCUMENTS …"),
        ("No bulk-access anomaly", "> 25 distinct patients / hour ⇒ deny"),
    ]
    y = 20
    for i, (t, s) in enumerate(checks):
        b.append(box(20, y, 430, 44, t, s, "blue" if i >= 4 else "white", size=12.5, sub_size=10.5, align="left"))
        b.append(path(f"M450,{y + 22} L520,{y + 22}", color=RED, marker="arrow-red", dashed=True))
        y += 52
    b.append(text(20, 394, "Checks run top to bottom; the first failure denies.", 11, 400, MUTED))
    b.append(box(525, 20, 355, 340, "Generic 403 “Access not permitted.”", [
        "Same response for an unknown patient",
        "and an unconsented one, so the API",
        "cannot be used to discover patients.",
        "",
        "DOCTOR_ACCESS_DENIED is audited",
        "with a reason code: ROLE, NO_CONSENT,",
        "SCOPE, RATE_LIMIT, ANOMALY,",
        "UNKNOWN_OBJECT.",
        "",
        "All checks pass ⇒ DOCTOR_ACCESS_GRANTED",
        "is audited and the data is returned.",
    ], "red", size=13, sub_size=11.5))
    b.append(text(20, 412, "security/consent.py · authorize_doctor() — called by every patient-data route in routes/doctor.py", 11, 600, TEAL))
    save("11_authorization", svg(W, H, "".join(b), "Consent-based authorization decision"))


# ============================================================
# 12. EVIDENCE TOKEN
# ============================================================

def evidence_token():
    W, H = 900, 300
    b = []
    b.append(box(20, 40, 180, 90, "Request token", ["POST /doctor/evidence/", "{id}/view-token", "needs SOURCE_DOCUMENTS"], "white", sub_size=10.5))
    b.append(box(240, 40, 180, 90, "Token issued", ["random 32 bytes; only", "HMAC stored; bound to", "session + doctor; 60 s"], "blue", sub_size=10.5))
    b.append(box(460, 40, 180, 90, "First view", ["GET …/evidence/view/{t}", "used_at set; consent", "re-checked now"], "teal", sub_size=10.5))
    b.append(box(680, 40, 200, 90, "Rendered region", ["worker renders one page,", "highlight + watermark", "(doctor ref + UTC time)"], "teal", sub_size=10.5))
    b.append(arrow(200, 85, 237, 85))
    b.append(arrow(420, 85, 457, 85))
    b.append(arrow(640, 85, 677, 85))
    b.append(box(460, 170, 420, 100, "Rejected (403, EVIDENCE_TOKEN_REJECTED)", [
        "TOKEN_REUSED — second use of the same URL",
        "TOKEN_EXPIRED — after 60 seconds",
        "SESSION_MISMATCH — another session / doctor",
        "consent revoked between issue and view",
    ], "red", size=12.5, sub_size=11))
    b.append(arrow(550, 130, 550, 167, color=RED, marker="arrow-red"))
    b.append(box(20, 170, 400, 100, "Delivery", [
        "PNG with Cache-Control: no-store, private",
        "held as an in-memory blob URL (SecureEvidence.tsx)",
        "no endpoint serves the original file",
    ], "slate", size=12.5, sub_size=11))
    save("12_evidence_token", svg(W, H, "".join(b), "Single-use evidence token"))


# ============================================================
# 13. AUDIT CHAIN
# ============================================================

def audit_chain():
    W, H = 900, 380
    b = []
    xs = [20, 240, 460, 680]
    labels = [("#1", "GENESIS 000…0"), ("#2", "hash(#1)"), ("#3", "hash(#2)"), ("#4", "hash(#3)")]
    for x, (seq, prev) in zip(xs, labels):
        b.append(box(x, 30, 190, 110, f"Event {seq}", ["seq · time · actor · action", "object · result · reason", f"prev_hash = {prev}", "event_hash = SHA-256(…)", "mac = HMAC(audit key)"], "white", size=13, sub_size=10.5))
    for x in xs[1:]:
        b.append(arrow(x - 30, 85, x - 3, 85, color=TEAL, marker="arrow-teal"))
    b.append(box(680, 160, 190, 50, "Anchor (identity DB)", "head_seq + head_hash", "slate", size=12, sub_size=10.5))
    b.append(arrow(775, 140, 775, 157))
    b.append(text(20, 245, "verify_chain() walks every event in order and reports the FIRST failure:", 12.5, 700, INK))
    rows = [
        ("SEQUENCE_GAP", "an event was deleted or inserted"),
        ("PREVIOUS_HASH_MISMATCH", "events were reordered or relinked"),
        ("EVENT_HASH_MISMATCH", "a stored field was modified"),
        ("MAC_MISMATCH", "hash recomputed without the audit key"),
        ("ANCHOR_MISMATCH", "the tail was truncated"),
    ]
    y = 270
    for code, meaning in rows:
        b.append(text(40, y, code, 11.5, 700, RED, mono=True))
        b.append(text(260, y, meaning, 11.5, 400, INK))
        y += 20
    b.append(box(560, 240, 320, 120, "Also enforced", ["SQLite triggers block UPDATE and", "DELETE on audit_events", "events hold refs and codes only", "(usr_…, pt_…, doc_…), never PHI", "auditor/admin: GET /audit/events"], "teal", size=12.5, sub_size=10.5))
    save("13_audit_chain", svg(W, H, "".join(b), "Tamper-evident audit chain"))


# ============================================================
# 14. TAMPER DEMO
# ============================================================

def tamper_demo():
    W, H = 900, 210
    b = []
    steps = [
        ("Verify", "valid: true", "VERIFIED", "green"),
        ("Tamper (demo)", "POST …/audit/tamper", "one result flipped", "amber"),
        ("Verify", "valid: false · broken_at", "EVENT_HASH_MISMATCH", "red"),
        ("Restore (demo)", "POST …/audit/restore", "original value back", "amber"),
        ("Verify", "valid: true", "VERIFIED", "green"),
    ]
    x = 20
    for i, (t, s1, s2, tone) in enumerate(steps):
        b.append(box(x, 40, 158, 90, t, [s1, s2], tone, size=13, sub_size=10.5))
        if i:
            b.append(arrow(x - 18, 85, x - 3, 85))
        x += 176
    b.append(text(20, 165, "Demo only: auditor/admin role, JEEVAFLOW_DEMO_MODE on (404 otherwise). It simulates an attacker with direct database write", 11.5, 400, MUTED))
    b.append(text(20, 183, "access. Verified live on 2 Oct 2026: tamper seq 5 ⇒ valid false, broken_at_seq 5, EVENT_HASH_MISMATCH; after restore ⇒ valid true.", 11.5, 400, MUTED))
    save("14_tamper_demo", svg(W, H, "".join(b), "Audit tamper demonstration"))


# ============================================================
# 15. REVOCATION
# ============================================================

def revocation():
    W, H = 900, 200
    b = []
    steps = [
        ("Active consent", "doctor reads form", "teal"),
        ("Patient revokes", "POST …/revoke", "red"),
        ("Next doctor request", "authorize_doctor()", "white"),
        ("No active consent", "query finds none", "white"),
        ("403 + audit", "DOCTOR_ACCESS_DENIED", "red"),
    ]
    x = 20
    for i, (t, s, tone) in enumerate(steps):
        b.append(box(x, 40, 158, 70, t, s, tone, size=13, sub_size=11))
        if i:
            b.append(arrow(x - 18, 75, x - 3, 75))
        x += 176
    b.append(text(20, 150, "No caches or long-lived grants: the patient list, form, journey, loops, conflicts, evidence and view tokens all re-run", 11.5, 400, MUTED))
    b.append(text(20, 168, "the consent query per request, so revocation takes effect on the very next call (live check: 403 immediately after revoke).", 11.5, 400, MUTED))
    save("15_revocation", svg(W, H, "".join(b), "Access revocation"))


# ============================================================
# 16. DEFENSE IN DEPTH
# ============================================================

def defense_layers():
    W, H = 900, 660
    layers = [
        ("1", "Communication security", "Generic replies only; media deleted from Twilio; unknown numbers never become patients", "amber"),
        ("2", "Webhook verification", "X-Twilio-Signature (fails closed), replay, account, freshness, rate limits", "amber"),
        ("3", "Patient authentication", "Transaction-bound OTP: HMAC stored, 5 min, single use, 5 attempts + lockout", "blue"),
        ("4", "Consent", "Patient-chosen doctor, scopes, 1/7/30-day expiry; revoke / end now", "blue"),
        ("5", "Document security", "Validation, isolated worker, structural PDF scan, sanitise, malware scan, quarantine", "teal"),
        ("6", "Encryption / vault", "AES-256-GCM per document (envelope), field-level encryption, separate identity DB", "teal"),
        ("7", "Provenance", "Quote-or-reject; every fact has quote, page, bbox, confidence, document hash", "purple"),
        ("8", "Doctor MFA", "scrypt password + TOTP with replay block; lockout; server-side sessions + CSRF", "blue"),
        ("9", "Authorization", "Deny-by-default roles + consent + scope checked on every request; generic 403", "blue"),
        ("10", "Evidence controls", "Single-use 60 s session-bound token, watermarked render, no download", "teal"),
        ("11", "Audit chain", "SHA-256 hash chain + HMAC, append-only triggers, anchored head, auditor verify", "slate"),
        ("12", "Retention", "Crypto-shredding after 90 days and for unverified uploads; delete = destroy key first", "slate"),
    ]
    b = []
    y = 15
    for i, (n, name, detail, tone) in enumerate(layers):
        inset = i * 8
        fill, stroke = PALETTE[tone]
        b.append(f'<rect x="{20 + inset}" y="{y}" width="{860 - 2 * inset}" height="44" rx="8" fill="{fill}" stroke="{stroke}" stroke-width="1.3"/>')
        b.append(f'<circle cx="{46 + inset}" cy="{y + 22}" r="13" fill="{stroke}"/>')
        b.append(text(46 + inset, y + 26.5, n, 11.5, 700, "#FFFFFF", "middle"))
        b.append(text(70 + inset, y + 19, name, 13, 700, stroke))
        b.append(text(70 + inset, y + 35, detail, 11, 400, INK))
        y += 51
    b.append(text(450, y + 12, "All twelve layers are implemented in the repository; demo-grade parts are named in Section 29.", 11.5, 600, TEAL, "middle"))
    save("16_defense_in_depth", svg(W, H, "".join(b), "Defense in depth"))


if __name__ == "__main__":
    architecture()
    whatsapp_boundary()
    otp_sequence()
    consent_lifecycle()
    document_lifecycle()
    provenance()
    doctor_form_flow()
    conflict()
    loops()
    mfa()
    authorization()
    evidence_token()
    audit_chain()
    tamper_demo()
    revocation()
    defense_layers()
