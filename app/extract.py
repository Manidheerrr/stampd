"""Stages 1-2: read the PDF (native text layer or OCR) and pull structured fields out of it.

Two extractors:
  * Rules parser (default, no API key needed): label-driven, tolerant of common Indian invoice layouts
    (Invoice No / Bill # / Inv. No., Grand Total / Amount Due / Total Amount, CGST+SGST split, ₹ / Rs. / INR,
    dd-Mon-yyyy / dd/mm/yyyy / yyyy-mm-dd / "6 Nov 2026" dates, optional HSN column in line items).
  * LLM (optional): Claude when ANTHROPIC_API_KEY is set, or Google Gemini (free tier) when GEMINI_API_KEY is set.
    Model output is cleaned, gaps are filled from the rules parser, and any error falls back to rules.
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


# ---------------------------------------------------------------- optional LLM extractors (read only, never decide)
LLM_SCHEMA = ("vendor_name, invoice_no, invoice_date (exactly as printed), invoice_date_iso (yyyy-mm-dd), po_ref, currency (ISO code), "
              "subtotal (number), tax (number: sum of all GST components), total (number), bank_account, ifsc, "
              "line_items (list of {description, hsn, qty (number), rate (number), amount (number)}), "
              "notes (list of notable remarks, e.g. bank-change requests)")
LLM_PROMPT = ("You extract fields from a vendor invoice for an accounts-payable system. Return ONLY a JSON object with these keys: "
              + LLM_SCHEMA + ". Use null when a field is not printed. Never guess, and never compute a value that is not printed.\n\nINVOICE TEXT:\n")
GEMINI_MODELS = [m for m in [os.environ.get("GEMINI_MODEL"), "gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest"] if m]


def _clean_llm(f, text):
    """Normalise model output, then fill any gaps from the rules parser so one missed field never blanks a run."""
    if not isinstance(f, dict): raise ValueError("model did not return a JSON object")
    for k in ("subtotal", "tax", "total"):
        f[k] = to_num(f[k]) if f.get(k) not in (None, "") else None
    items = []
    for it in f.get("line_items") or []:
        if isinstance(it, dict) and it.get("description"):
            q = to_num(it.get("qty")); q = int(q) if q is not None and float(q).is_integer() else q
            items.append({"description": str(it["description"]), "hsn": it.get("hsn"), "qty": q, "rate": to_num(it.get("rate")), "amount": to_num(it.get("amount"))})
    f["line_items"] = [i for i in items if None not in (i["qty"], i["amount"])]
    for k in ("vendor_name", "invoice_no", "invoice_date", "po_ref", "bank_account", "ifsc", "currency"):
        if f.get(k) is not None: f[k] = str(f[k]).strip() or None
    if f.get("po_ref"): f["po_ref"] = re.sub(r"\s", "-", f["po_ref"].upper())
    rules = parse_rules(text); f["_filled_by_rules"] = []
    for k in ("vendor_name", "invoice_no", "invoice_date", "po_ref", "subtotal", "tax", "total", "bank_account", "ifsc"):
        if f.get(k) in (None, "") and rules.get(k) not in (None, ""):
            f[k] = rules[k]; f["_filled_by_rules"].append(k)
    if not f["line_items"]: f["line_items"] = rules["line_items"]
    f["currency"] = (f.get("currency") or rules["currency"] or "INR").upper()
    f["invoice_date_iso"] = f.get("invoice_date_iso") if parse_date(f.get("invoice_date_iso") or "") else parse_date(f.get("invoice_date"))
    f.setdefault("notes", rules["notes"]); f.setdefault("_derived", [])
    return f


def parse_claude(text):
    import anthropic
    msg = anthropic.Anthropic().messages.create(model=os.environ.get("LLM_MODEL", "claude-sonnet-5-5"), max_tokens=2000,
                                               messages=[{"role": "user", "content": LLM_PROMPT + text}])
    raw = msg.content[0].text
    return _clean_llm(json.loads(raw[raw.find("{"): raw.rfind("}") + 1]), text)


def _gemini_call(model, prompt, timeout=60):
    import urllib.request
    cfg = {"temperature": 0, "responseMimeType": "application/json"}
    if "2.5" in model or "flash-latest" in model:
        cfg["thinkingConfig"] = {"thinkingBudget": 0}  # extraction needs no reasoning; keeps latency low
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}], "generationConfig": cfg}).encode()
    req = urllib.request.Request(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", data=body,
                                 headers={"Content-Type": "application/json", "x-goog-api-key": os.environ["GEMINI_API_KEY"]})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    return data["candidates"][0]["content"]["parts"][0]["text"]


def parse_gemini(text):
    """Google Gemini via plain REST (free tier available). Tries a few model ids so a renamed model doesn't break it."""
    import urllib.error
    errors = []
    for model in GEMINI_MODELS:
        try:
            raw = _gemini_call(model, LLM_PROMPT + text)
            f = _clean_llm(json.loads(raw[raw.find("{"): raw.rfind("}") + 1]), text)
            f["_model"] = model
            return f
        except urllib.error.HTTPError as e:
            detail = ""
            try: detail = json.loads(e.read().decode()).get("error", {}).get("message", "")[:120]
            except Exception: pass
            errors.append(f"{model}: HTTP {e.code} {detail}".strip())
            if e.code in (401, 403, 429): break  # auth/quota problems won't be fixed by another model id
        except Exception as e:
            errors.append(f"{model}: {str(e)[:80]}")
    raise RuntimeError(" | ".join(errors) or "Gemini unavailable")


def llm_selfcheck():
    """Tiny live call per model, for the /api/llm-check diagnostics endpoint. Never returns the key."""
    if not os.environ.get("GEMINI_API_KEY"):
        return {"configured": False}
    import urllib.error
    res = {}
    for model in GEMINI_MODELS:
        t0 = __import__("time").time()
        try:
            out = _gemini_call(model, 'Return the JSON {"ok": true}', timeout=30)
            res[model] = {"ok": True, "secs": round(__import__("time").time() - t0, 1), "reply": out[:40]}
            break
        except urllib.error.HTTPError as e:
            msg = ""
            try: msg = json.loads(e.read().decode()).get("error", {}).get("message", "")[:160]
            except Exception: pass
            res[model] = {"ok": False, "http": e.code, "error": msg}
        except Exception as e:
            res[model] = {"ok": False, "error": str(e)[:120]}
    return {"configured": True, "models": res}


def extractor_name():
    mode = os.environ.get("EXTRACTOR", "auto")
    if mode == "rules": return "Rules parser"
    if os.environ.get("ANTHROPIC_API_KEY") and mode in ("auto", "claude"): return "Claude"
    if os.environ.get("GEMINI_API_KEY") and mode in ("auto", "gemini"): return "Gemini"
    return "Rules parser"


def extract(text):
    name = extractor_name()
    if name == "Rules parser":
        return parse_rules(text), name
    try:
        f = parse_claude(text) if name == "Claude" else parse_gemini(text)
        filled = f.get("_filled_by_rules") or []
        return f, name + (f" + rules for {', '.join(filled)}" if filled else "")
    except Exception as e:
        f = parse_rules(text); f["_llm_error"] = str(e)[:200]
        return f, f"Rules parser ({name} unavailable: {str(e)[:60]})"
