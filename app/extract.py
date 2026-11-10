"""Stages 1-2: read the PDF (native text layer or OCR) and pull structured fields out of it.

Two extractors:
  * Rules parser (default, no API key needed): label-driven, tolerant of common Indian invoice layouts
    (Invoice No / Bill # / Inv. No., Grand Total / Amount Due / Total Amount, CGST+SGST split, ₹ / Rs. / INR,
    dd-Mon-yyyy / dd/mm/yyyy / yyyy-mm-dd / "6 Nov 2026" dates, optional HSN column in line items).
  * Claude (optional): used automatically when ANTHROPIC_API_KEY is set. Falls back to rules on any error.
Extraction only *reads*. It never decides anything; decisions live in pipeline.py.
"""
import os, re, subprocess, tempfile, json, datetime
import pdfplumber

CUR = r"(?:₹|Rs\.?|INR)?\s*"
NUM = r"([\d,]+\.\d{2})"
AMT = r"([\d,]+(?:\.\d{1,2})?)"
MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


def to_num(s):
    try:
        return round(float(str(s).replace(",", "").strip()), 2)
    except Exception:
        return None


# ---------------------------------------------------------------- reading
def read_pdf(path):
    """Returns (text, mode, confidence, n_pages). mode is 'text' or 'ocr'."""
    try:
        with pdfplumber.open(path) as pdf:
            if len(pdf.pages) > 20:
                raise ValueError("Invoice has more than 20 pages; split it before processing.")
            text = "\n".join((p.extract_text() or "") for p in pdf.pages)
            n_pages = len(pdf.pages)
    except ValueError:
        raise
    except Exception as e:
        msg = str(e).lower()
        if "password" in msg or "encrypt" in msg:
            raise ValueError("PDF is password-protected. Ask the vendor for an unlocked copy.")
        raise ValueError("PDF could not be opened. It may be corrupted.")
    if len(text.strip()) > 40:
        return text, "text", 1.0, n_pages
    try:
        return _ocr(path) + (n_pages,)
    except Exception:
        # Machine without tesseract/poppler (e.g. a fresh Windows laptop): use OCR text pre-computed
        # by tesseract for the bundled scanned sample, so the local demo still runs.
        side = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples", "ocr_cache",
                            re.sub(r"^[0-9a-f]{6}_", "", os.path.basename(path)) + ".json")
        if os.path.exists(side):
            d = json.load(open(side))
            return d["text"], "ocr", d["confidence"], n_pages
        raise ValueError("Scanned PDF needs OCR, but Tesseract is not installed on this machine.")


def _ocr(path):
    """Single-pass OCR (text + confidence from one tesseract call) so it stays fast on small servers."""
    import pytesseract
    from PIL import Image
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["pdftoppm", "-r", "150", "-gray", "-png", path, os.path.join(d, "p")], check=True, capture_output=True)
        lines, confs = [], []
        for f in sorted(os.listdir(d)):
            data = pytesseract.image_to_data(Image.open(os.path.join(d, f)), config="--psm 6", output_type=pytesseract.Output.DICT)
            cur, key = [], None
            for i, w in enumerate(data["text"]):
                k = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
                if k != key and cur:
                    lines.append(" ".join(cur)); cur = []
                key = k
                if w.strip():
                    cur.append(w)
                    if float(data["conf"][i]) >= 0: confs.append(float(data["conf"][i]))
            if cur: lines.append(" ".join(cur))
    conf = (sum(confs) / len(confs) / 100) if confs else 0.0
    return "\n".join(lines), "ocr", round(conf, 2)


# ---------------------------------------------------------------- helpers
def _line_match(lines, label_re, value_re):
    for ln in lines:
        m = re.search(label_re + r"\s*[:\-]?\s*" + value_re, ln, re.I)
        if m:
            return m.group(1).strip()
    return None


def _last_amount(ln):
    nums = re.findall(r"[\d,]*\d\.\d{2}|\d[\d,]{2,}", ln)
    return to_num(nums[-1]) if nums else None


def _labeled_amount(lines, label_re, exclude=None):
    for ln in lines:
        if re.search(label_re, ln, re.I) and not (exclude and re.search(exclude, ln, re.I)):
            v = _last_amount(ln)
            if v is not None:
                return v
    return None


def parse_date(s):
    """Normalise a date string to ISO yyyy-mm-dd (None if not understood). Day-first, as on Indian invoices."""
    if not s:
        return None
    s = s.strip().replace(",", " ")
    try:
        if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
            return datetime.date.fromisoformat(s).isoformat()
        m = re.match(r"^(\d{1,2})[-/. ]+([A-Za-z]{3,9})[-/. ]+(\d{4})$", s)
        if m:
            return datetime.date(int(m[3]), MONTHS[m[2][:3].lower()], int(m[1])).isoformat()
        m = re.match(r"^([A-Za-z]{3,9})\s+(\d{1,2})\s+(\d{4})$", s)
        if m:
            return datetime.date(int(m[3]), MONTHS[m[1][:3].lower()], int(m[2])).isoformat()
        m = re.match(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})$", s)
        if m:
            return datetime.date(int(m[3]), int(m[2]), int(m[1])).isoformat()
    except Exception:
        return None
    return None


DATE_RE = (r"(\d{1,2}[-/. ][A-Za-z]{3,9}[-/. ,]+\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{4}"
           r"|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})")


# ---------------------------------------------------------------- rules parser
def parse_rules(text):
    lines = [re.sub(r"\s+", " ", l).strip() for l in text.splitlines() if l.strip()]
    f = {"_derived": []}

    # Vendor: first meaningful line (skip bare "INVOICE"/"TAX INVOICE" headers and "From:" labels).
    vendor = None
    for ln in lines[:4]:
        cand = re.sub(r"^(from|supplier|vendor)\s*:\s*", "", ln, flags=re.I)
        cand = re.sub(r"\s*(ORIGINAL FOR RECIPIENT|TAX\s+INVOICE|INVOICE)\s*$", "", cand, flags=re.I).strip()
        if cand and not re.fullmatch(r"(tax\s+)?invoice|original.*", cand, re.I):
            vendor = cand; break
    f["vendor_name"] = vendor

    f["invoice_no"] = _line_match(
        lines, r"(?:Invoice\s*(?:No\.?|Number|#)|Inv\.?\s*(?:No\.?|#)|Bill\s*(?:No\.?|#)|Invoice)",
        r"(?!Date)([A-Z0-9][A-Z0-9/\-]{2,24})")
    raw_date = _line_match(lines, r"(?:Invoice\s*Date|Inv\.?\s*Date|Bill\s*Date|Dated|Date)", DATE_RE)
    f["invoice_date"] = raw_date
    f["invoice_date_iso"] = parse_date(raw_date)

    m = re.search(r"\bPO[-\s]?\d{4}[-\s]?\d{4}\b", text, re.I)
    f["po_ref"] = re.sub(r"\s", "-", m.group(0).upper()) if m else None

    f["currency"] = "USD" if re.search(r"\bUSD\b|US\$|\$\s?\d", text) else "EUR" if re.search(r"\bEUR\b|€", text) else "INR"

    f["subtotal"] = _labeled_amount(lines, r"^(Sub\s*-?\s*total|Taxable\s*(Value|Amount)|Amount\s*before\s*tax|Net\s*Amount)\b")
    total_tax = _labeled_amount(lines, r"^Total\s*(Tax|GST)\b")
    if total_tax is not None:
        tax = total_tax
    else:
        tax = None
        for ln in lines:
            if re.search(r"^(CGST|SGST|UTGST|IGST|GST|VAT|Tax)\b", ln, re.I) and not re.search(r"GSTIN|taxable|incl", ln, re.I):
                v = _last_amount(ln)
                if v is not None:
                    tax = round((tax or 0) + v, 2)
    f["tax"] = tax
    f["total"] = (_labeled_amount(lines, r"^(Grand\s*Total|Amount\s*Due|Total\s*Payable|Invoice\s*Total|Total\s*Amount|Net\s*Payable|Balance\s*Due|Total\s*Invoice\s*Value)\b")
                  or _labeled_amount(lines, r"^Total\b", exclude=r"Sub|Tax|GST|Qty"))

    # Fill one missing amount from the other two, and say so (shown as 'derived' in the UI).
    if f["tax"] is None and f["subtotal"] is not None and f["total"] is not None:
        f["tax"] = round(f["total"] - f["subtotal"], 2); f["_derived"].append("tax")
    if f["subtotal"] is None and f["tax"] is not None and f["total"] is not None:
        f["subtotal"] = round(f["total"] - f["tax"], 2); f["_derived"].append("subtotal")

    f["bank_account"] = _line_match(lines, r"(?:Account\s*(?:No\.?|Number)|A/c\s*(?:No\.?)?)", r"(\d{9,18})")
    f["ifsc"] = _line_match(lines, r"IFSC(?:\s*Code)?", r"([A-Z]{4}0[A-Z0-9]{6})")

    items = []
    row = re.compile(r"^(?:\d{1,3}\s+)?(.+?)\s+(?:(\d{4,8})\s+)?(\d+(?:\.\d+)?)\s*(?:nos|pcs|units?|kg|hrs?|boxes)?\s+"
                     + CUR + r"([\d,]+\.\d{2})\s+" + CUR + r"([\d,]+\.\d{2})$", re.I)
    for ln in lines:
        mm = row.match(ln)
        if mm and not re.search(r"total|gst|tax", mm.group(1), re.I):
            qty = float(mm.group(3)); qty = int(qty) if qty.is_integer() else qty
            items.append({"description": mm.group(1), "hsn": mm.group(2), "qty": qty,
                          "rate": to_num(mm.group(4)), "amount": to_num(mm.group(5))})
    f["line_items"] = items
    f["notes"] = [ln for ln in lines if re.search(r"bank|remit|milestone|overdue|reminder|urgent", ln, re.I)
                  and not re.search(r"^Bank Details$|Account No|IFSC|Bank\s*:", ln, re.I)]
    return f


# ---------------------------------------------------------------- optional LLM extractor
def parse_llm(text):
    import anthropic
    client = anthropic.Anthropic()
    schema = ("vendor_name, invoice_no, invoice_date (as printed), invoice_date_iso (yyyy-mm-dd), po_ref, currency (ISO code), "
              "subtotal (number), tax (number, sum of all GST components), total (number), bank_account, ifsc, "
              "line_items (list of {description, hsn, qty, rate, amount}), notes (list of notable remarks, e.g. bank change requests)")
    msg = client.messages.create(
        model=os.environ.get("LLM_MODEL", "claude-sonnet-5-5"), max_tokens=2000,
        messages=[{"role": "user", "content":
                   "You are extracting fields from a vendor invoice for an accounts-payable system. Return ONLY strict JSON "
                   f"with these keys: {schema}. Use null when a field is absent. Never guess or compute values that are not printed."
                   f"\n\nINVOICE TEXT:\n{text}"}])
    raw = msg.content[0].text
    f = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
    f.setdefault("_derived", [])
    if not f.get("invoice_date_iso"):
        f["invoice_date_iso"] = parse_date(f.get("invoice_date"))
    f.setdefault("currency", "INR")
    return f


def extractor_name():
    return "Claude" if os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("EXTRACTOR", "auto") != "rules" else "Rules parser"


def extract(text):
    if extractor_name() == "Claude":
        try:
            return parse_llm(text), "Claude"
        except Exception as e:
            f = parse_rules(text); f["_llm_error"] = str(e)[:200]
            return f, "Rules parser (Claude unavailable, fell back)"
    return parse_rules(text), "Rules parser"
