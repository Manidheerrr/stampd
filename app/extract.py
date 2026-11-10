"""Stage 1-2: read the PDF (text layer or OCR) and pull structured fields out of it."""
import os, re, subprocess, tempfile, json
import pdfplumber

NUM = r"([\d,]+\.\d{2})"


def to_num(s):
    try:
        return float(s.replace(",", ""))
    except Exception:
        return None


def read_pdf(path):
    """Returns (text, mode, ocr_confidence). mode is 'text' or 'ocr'."""
    with pdfplumber.open(path) as pdf:
        pages = pdf.pages
        text = "\n".join((p.extract_text() or "") for p in pages)
        n_pages = len(pages)
    if len(text.strip()) > 40:
        return text, "text", 1.0, n_pages
    try:
        return _ocr(path) + (n_pages,)
    except Exception:
        # Machine without tesseract/poppler (e.g. a fresh Windows laptop): use OCR text pre-computed
        # by tesseract for bundled samples, so the demo still runs. Flagged clearly in the UI.
        side = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples", "ocr_cache",
                            re.sub(r"^[0-9a-f]{6}_", "", os.path.basename(path)) + ".json")
        if os.path.exists(side):
            d = json.load(open(side))
            return d["text"], "ocr", d["confidence"], n_pages
        raise RuntimeError("Scanned PDF needs OCR, but Tesseract is not installed on this machine.")


def _ocr(path):
    import pytesseract
    from PIL import Image
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["pdftoppm", "-r", "220", "-png", path, os.path.join(d, "p")], check=True, capture_output=True)
        texts, confs = [], []
        for f in sorted(os.listdir(d)):
            im = Image.open(os.path.join(d, f))
            texts.append(pytesseract.image_to_string(im))
            data = pytesseract.image_to_data(im, output_type=pytesseract.Output.DICT)
            confs += [float(c) for c, w in zip(data["conf"], data["text"]) if w.strip() and float(c) >= 0]
    conf = (sum(confs) / len(confs) / 100) if confs else 0.0
    return "\n".join(texts), "ocr", round(conf, 2)


def _line_match(lines, label_re, value_re):
    for ln in lines:
        m = re.search(label_re + r"\s*[:\-]?\s*" + value_re, ln, re.I)
        if m:
            return m.group(1).strip()
    return None


def parse_rules(text):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    f = {}
    first = lines[0] if lines else ""
    f["vendor_name"] = re.sub(r"\s*(TAX\s+)?INVOICE\s*$", "", first, flags=re.I).strip() or None
    f["invoice_no"] = _line_match(lines, r"(?:Invoice\s*(?:No\.?|Number|#)|Bill\s*(?:No\.?|#)|Inv\.?\s*No\.?)",
                                  r"([A-Z0-9][A-Z0-9/\-]{2,24})")
    f["invoice_date"] = _line_match(lines, r"(?:Invoice\s*Date|Dated|Date)",
                                    r"(\d{1,2}[-/ ][A-Za-z]{3}[-/ ]\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})")
    m = re.search(r"PO[-\s]?\d{4}[-\s]?\d{4}", text, re.I)
    f["po_ref"] = re.sub(r"\s", "-", m.group(0).upper()) if m else None
    f["subtotal"] = to_num(_line_match(lines, r"(?:Sub\s*-?\s*total|Taxable\s*Value)", NUM) or "")
    tax = None
    for ln in lines:
        if re.search(r"\b(GST|IGST|CGST|VAT|Tax)\b", ln, re.I) and not re.search(r"total|GSTIN|taxable", ln, re.I):
            mm = re.search(NUM + r"\s*$", ln)
            if mm:
                tax = (tax or 0) + to_num(mm.group(1))
    f["tax"] = tax
    f["total"] = to_num(_line_match(lines, r"(?:Grand\s*Total|Amount\s*Due|Total\s*Payable|Invoice\s*Total)[^\d]*?", NUM) or "")
    f["bank_account"] = _line_match(lines, r"(?:Account\s*(?:No\.?|Number)|A/c\s*No\.?)", r"(\d{9,18})")
    f["ifsc"] = _line_match(lines, r"IFSC", r"([A-Z]{4}0[A-Z0-9]{6})")
    items = []
    for ln in lines:
        mm = re.match(r"^(.+?)\s+(\d+)\s+" + NUM + r"\s+" + NUM + r"$", ln)
        if mm:
            items.append({"description": mm.group(1), "qty": int(mm.group(2)), "rate": to_num(mm.group(3)), "amount": to_num(mm.group(4))})
    f["line_items"] = items
    notes = [ln for ln in lines if re.search(r"bank|remit|milestone|overdue|reminder", ln, re.I)
             and not re.search(r"^Bank Details$|Account No|IFSC", ln, re.I)]
    f["notes"] = notes
    return f


def parse_llm(text):
    """Optional: Claude-based extraction. Enabled only when EXTRACTOR=llm and ANTHROPIC_API_KEY are set."""
    import anthropic
    client = anthropic.Anthropic()
    schema = ("vendor_name, invoice_no, invoice_date, po_ref, subtotal (number), tax (number), total (number), "
              "bank_account, ifsc, line_items (list of {description, qty, rate, amount}), notes (list of strings)")
    msg = client.messages.create(
        model=os.environ.get("LLM_MODEL", "claude-sonnet-5-5"), max_tokens=1500,
        messages=[{"role": "user", "content": f"Extract these invoice fields as strict JSON (null when absent, never guess): {schema}\n\nINVOICE TEXT:\n{text}"}])
    raw = msg.content[0].text
    return json.loads(raw[raw.find("{"): raw.rfind("}") + 1])


def extract(text):
    if os.environ.get("EXTRACTOR") == "llm" and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return parse_llm(text), "Claude (LLM)"
        except Exception as e:
            f = parse_rules(text); f["_llm_error"] = str(e)[:200]
            return f, "Rules (LLM failed, fell back)"
    return parse_rules(text), "Rules parser"
