"""The decision pipeline.

Design principle: extraction (OCR / parser / optional LLM) only *reads* the invoice. Every decision below is a
deterministic rule, so the same invoice always gets the same answer and every answer can be explained.

Each stage is a small function that adds checks (shown in the UI) and flags (which drive the decision):
    severity "reject" -> REJECT,  "hold" -> HOLD for a human,  "info" -> noted, still auto-approvable.
"""
import os, re, time, difflib, datetime
from .extract import read_pdf, extract

STAGES = [
    ("ingest", "Ingest"), ("extract", "Extract fields"), ("validate", "Validate"),
    ("vendor", "Vendor check"), ("po", "PO match"), ("receipt", "Goods receipt (3-way)"), ("duplicate", "Duplicate check"),
    ("decision", "Decision"),
]
PACE = float(os.environ.get("STAMPD_PACE", "0.45"))  # seconds per stage so the live view is followable on a demo
GST_SLABS = (0, 5, 12, 18, 28)


def norm_no(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def inr(x):
    return "—" if x is None else f"₹{x:,.2f}"


def similar(a, b):
    return difflib.SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


def worst(checks):
    s = [c["ok"] for c in checks]
    return "fail" if "fail" in s else "warn" if "warn" in s else "pass"


class Ctx:
    """State shared by the stages of one run."""

    def __init__(self, path, filename, master, ledger, emit, source):
        self.path, self.filename, self.master, self.ledger, self.emit, self.source = path, filename, master, ledger, emit, source
        self.pol = master["policy"]
        self.flags, self.checks = [], []
        self.out = {"filename": filename}
        self.text = ""; self.f = {}; self.vendor = None; self.po = None

    def check(self, label, ok, detail):
        self.checks.append({"label": label, "ok": ok, "detail": detail})

    def flag(self, code, severity, message):
        self.flags.append({"code": code, "severity": severity, "message": message})

    @property
    def items(self):
        return self.f.get("line_items") or []

    @property
    def total(self):
        return self.f.get("total")


# ---------------------------------------------------------------- 1. ingest
def stage_ingest(x):
    text, mode, conf, pages = read_pdf(x.path)
    x.text = text
    x.check("File received", "pass", f"{x.filename} · {pages} page(s)")
    x.check("Readable text layer", "pass" if mode == "text" else "warn",
            "Native PDF text" if mode == "text" else f"No text layer, scanned image. Ran OCR (confidence {conf:.0%})")
    if mode == "ocr" and conf < x.pol["ocr_review_threshold"]:
        x.flag("LOW_OCR_CONFIDENCE", "hold", f"OCR confidence {conf:.0%} is below the {x.pol['ocr_review_threshold']:.0%} review threshold.")
    x.out.update(read_mode=mode, ocr_confidence=conf, raw_text=text)
    return "Scanned → OCR" if mode == "ocr" else "Native PDF"


# ---------------------------------------------------------------- 2. extract
def stage_extract(x):
    f, engine = extract(x.text)
    x.f = f; x.out["fields"] = f; x.out["extractor"] = engine
    for k, label in [("vendor_name", "Vendor"), ("invoice_no", "Invoice no."), ("invoice_date", "Invoice date"), ("po_ref", "PO reference"),
                     ("subtotal", "Subtotal"), ("tax", "Tax"), ("total", "Total"), ("bank_account", "Bank a/c")]:
        v = f.get(k)
        present = v not in (None, "")
        shown = inr(v) if k in ("subtotal", "tax", "total") and present else v
        x.check(label, "pass" if present else ("warn" if k == "po_ref" else "fail"), shown if present else "Not found on invoice")
    x.check("Line items", "pass" if x.items else "warn", f"{len(x.items)} row(s)")
    x.check("Currency", "pass" if f.get("currency", "INR") == "INR" else "warn", f.get("currency", "INR"))
    for k in f.get("_derived") or []:
        x.check(f"{k.title()} (derived)", "warn", "Not printed; computed from the other two amounts")
    return f"{sum(1 for c in x.checks if c['ok'] == 'pass')}/{len(x.checks)} fields · {engine}"


# ---------------------------------------------------------------- 3. validate
def stage_validate(x):
    f, pol = x.f, x.pol
    missing = [k for k in ("invoice_no", "invoice_date", "total") if not f.get(k)]
    for k in missing:
        x.flag("MISSING_" + k.upper(), "hold", f"The {k.replace('_', ' ')} could not be found on the invoice.")
    x.check("Mandatory fields present", "fail" if missing else "pass", "Missing: " + ", ".join(missing) if missing else "Invoice no., date, total")

    if x.items and f.get("subtotal") is not None:
        s = round(sum(i["amount"] for i in x.items), 2); ok = abs(s - f["subtotal"]) <= pol["math_tolerance_abs"]
        x.check("Line items add up to subtotal", "pass" if ok else "fail", f"{inr(s)} vs {inr(f['subtotal'])}")
        if not ok: x.flag("MATH_LINES", "hold", f"Line items sum to {inr(s)} but subtotal says {inr(f['subtotal'])}.")
    if None not in (f.get("subtotal"), f.get("tax"), f.get("total")):
        s = round(f["subtotal"] + f["tax"], 2); ok = abs(s - f["total"]) <= pol["math_tolerance_abs"]
        x.check("Subtotal + tax = total", "pass" if ok else "fail", f"{inr(s)} vs {inr(f['total'])}")
        if not ok: x.flag("MATH_TOTAL", "hold", f"Subtotal + tax = {inr(s)}, but the invoice total is {inr(f['total'])}.")
        rate = f["tax"] / f["subtotal"] * 100 if f["subtotal"] else 0
        x.check("Tax rate is a valid GST slab", "pass" if any(abs(rate - r) < 0.05 for r in GST_SLABS) else "warn", f"{rate:.1f}%")

    iso = f.get("invoice_date_iso")
    if iso:
        age = (datetime.date.today() - datetime.date.fromisoformat(iso)).days
        if age < -1:
            x.check("Invoice date is not in the future", "fail", f"{iso} is {-age} days ahead")
            x.flag("FUTURE_DATED", "hold", f"Invoice is dated {iso}, which is in the future. Likely a typo or a pre-billed invoice.")
        elif age > 180:
            x.check("Invoice is recent", "warn", f"{age} days old")
            x.flag("STALE_INVOICE", "info", f"Invoice is {age} days old. Check it was not already settled offline.")
        else:
            x.check("Invoice date is plausible", "pass", f"{iso} · {age} day(s) ago")
    elif f.get("invoice_date"):
        x.check("Invoice date readable", "warn", f"Could not interpret '{f['invoice_date']}'")

    if f.get("currency", "INR") != pol["currency"]:
        x.check(f"Currency is {pol['currency']}", "fail", f"Invoice is in {f.get('currency')}")
        x.flag("FOREIGN_CURRENCY", "hold", f"Invoice is billed in {f.get('currency')}. POs are in {pol['currency']}, so treasury must apply the payment-date FX rate before matching.")
    return "Arithmetic, dates & completeness"


# ---------------------------------------------------------------- 4. vendor
def find_vendor(x):
    name_core = re.sub(r"\b(pvt|private|ltd|limited|llp|inc)\b\.?", "", (x.f.get("vendor_name") or "").lower()).strip()
    best = max(x.master["vendors"], key=lambda v: similar(name_core, v["name"]))
    score = similar(name_core, best["name"])
    if score >= 0.82:
        return best, score
    head = x.text.lower()[:600]  # vendor name not on the first line: look for a known name in the header
    for v in x.master["vendors"]:
        if v["name"].lower() in head:
            x.f["vendor_name"] = x.f.get("vendor_name") or v["name"]
            return v, 1.0
    return None, score


def stage_vendor(x):
    vendor, score = find_vendor(x)
    x.vendor = x.out["vendor"] = vendor
    if not vendor:
        x.check("Vendor in vendor master", "fail", f"'{x.f.get('vendor_name')}' not found")
        x.flag("VENDOR_UNKNOWN", "reject", f"'{x.f.get('vendor_name')}' is not in the vendor master. Stampd never pays a vendor that hasn't been onboarded.")
        return "Unknown vendor"
    x.check("Vendor in vendor master", "pass", f"{vendor['name']} ({vendor['id']}) · match {score:.0%}" + (" · sandbox test vendor" if vendor.get("sandbox") else ""))
    approved = vendor["status"] == "approved"
    x.check("Vendor approved for payment", "pass" if approved else "fail", vendor["status"].replace("_", " ").upper())
    if not approved:
        x.flag("VENDOR_NOT_APPROVED", "reject", f"{vendor['name']} is '{vendor['status'].replace('_', ' ')}'. Onboarding/KYC must be completed before any payment.")
    else:
        ba, on_file = x.f.get("bank_account"), vendor.get("bank_account") or ""
        if ba and on_file and ba != on_file:
            x.check("Bank account matches master", "fail", f"Invoice ••{ba[-4:]} vs master ••{on_file[-4:]}")
            x.flag("BANK_MISMATCH", "hold", f"Remit-to account ••{ba[-4:]} differs from the verified account ••{on_file[-4:]}. Classic payment-fraud pattern: verify by phone with a known contact before paying.")
        elif ba:
            x.check("Bank account matches master", "pass", f"••{ba[-4:]} verified")
        else:
            x.check("Bank account matches master", "warn", "No bank details on invoice; master account will be used")
    if x.source and x.source.get("from"):
        sender = x.source["from"].lower(); sdom = sender.split("@")[-1].strip("> ")
        vdom = vendor["contact"].split("@")[-1].lower()
        if sdom == vdom:
            x.check("Email sender matches vendor domain", "pass", f"{sender} · on file: @{vdom}")
        else:
            look = similar(sdom, vdom) >= 0.75
            x.check("Email sender matches vendor domain", "fail", f"{sender} vs @{vdom} on file" + (" (look-alike domain)" if look else ""))
            x.flag("SENDER_MISMATCH", "hold", (f"Sent from @{sdom}, a look-alike of the vendor's real domain @{vdom}. Typical business-email-compromise attack." if look
                                               else f"Sent from @{sdom}, but {vendor['name']} bills from @{vdom}.") + " Confirm with the vendor on a known number before paying.")
    return vendor["name"]


# ---------------------------------------------------------------- 5. PO match (2-way, amounts)
def stage_po(x):
    f, pol, vendor, total = x.f, x.pol, x.vendor, x.total
    pos = {p["po"]: p for p in x.master["purchase_orders"]}
    po = None
    if not vendor:
        x.check("PO lookup", "warn", "Skipped: vendor not recognised")
    elif f.get("po_ref"):
        po = pos.get(f["po_ref"])
        if not po:
            x.check("PO exists", "fail", f"{f['po_ref']} not found")
            x.flag("PO_NOT_FOUND", "hold", f"PO {f['po_ref']} does not exist in procurement.")
        elif po["vendor_id"] != vendor["id"]:
            x.check("PO belongs to this vendor", "fail", f"{po['po']} is issued to {po['vendor_id']}")
            x.flag("PO_VENDOR_MISMATCH", "hold", f"{po['po']} was issued to a different vendor."); po = None
        else:
            x.check("PO exists & belongs to vendor", "pass", f"{po['po']} · {po['description']}")
    else:
        open_pos = [p for p in x.master["purchase_orders"] if p["vendor_id"] == vendor["id"] and p["status"] == "open"]
        cands = sorted(open_pos, key=lambda p: abs(p["amount"] - (total or 0)))
        if cands and total and abs(cands[0]["amount"] - total) / cands[0]["amount"] * 100 <= pol["variance_tolerance_pct"]:
            po = cands[0]
            x.check("PO reference on invoice", "warn", "None printed")
            x.check("Inferred PO", "warn", f"{po['po']} ({po['description']}): closest of {len(open_pos)} open PO(s) for this vendor")
            x.flag("PO_INFERRED", "hold", f"No PO on the invoice. Best match is {po['po']} by vendor + amount. A human should confirm before payment.")
        else:
            x.check("PO reference on invoice", "fail", "None printed, and no open PO matches the amount")
            x.flag("PO_MISSING", "hold", "No PO reference and no open PO for this vendor matches. Ask the vendor for the PO number.")

    if po and total and f.get("currency", "INR") != pol["currency"]:
        x.check("Amount vs PO balance", "warn", f"Skipped: invoice is in {f.get('currency')}, PO is in {pol['currency']}")
    elif po and total:
        billed = sum(l["amount"] or 0 for l in x.ledger if l["po"] == po["po"] and l["status"] in ("paid", "approved")
                     and norm_no(l["invoice_no"]) != norm_no(f.get("invoice_no")))
        remaining = round(po["amount"] - billed, 2)
        if billed:
            x.check("Already billed against PO", "pass", f"{inr(billed)} of {inr(po['amount'])} (remaining {inr(remaining)})")
        var = total - remaining; var_pct = var / remaining * 100 if remaining > 0 else 999
        if var <= 0.005:
            x.check("Amount within PO balance", "pass", f"{inr(total)} ≤ {inr(remaining)}")
        elif var_pct <= pol["variance_tolerance_pct"] and var <= pol["variance_tolerance_abs"]:
            x.check("Amount within tolerance", "warn", f"Over by {inr(var)} ({var_pct:.2f}%), policy allows ≤{pol['variance_tolerance_pct']}% and ≤{inr(pol['variance_tolerance_abs'])}")
            x.flag("VARIANCE_OK", "info", f"Invoice is {var_pct:.2f}% ({inr(var)}) over the PO balance, inside the auto-approve tolerance.")
        elif billed:
            x.check("Amount within PO balance", "fail", f"Over by {inr(var)} (cumulative billing {inr(billed + total)} vs PO {inr(po['amount'])})")
            x.flag("PO_OVERBILLED", "hold", f"This is a split invoice. With {inr(billed)} already billed, it brings the PO to {inr(billed + total)}, which is {inr(var)} over the {inr(po['amount'])} PO. A PO amendment or a credit note is needed.")
        else:
            x.check("Amount within PO balance", "fail", f"Over by {inr(var)} ({var_pct:.1f}%)")
            x.flag("VARIANCE_HIGH", "hold", f"Invoice exceeds the PO by {inr(var)} ({var_pct:.1f}%), beyond tolerance.")
    x.po = x.out["po"] = po
    return po["po"] if po else "No PO matched"


# ---------------------------------------------------------------- 6. goods receipt (3-way, quantities and prices)
def stage_receipt(x):
    po = x.po
    if not po:
        x.check("3-way match", "warn", "Skipped: no PO matched"); return "Skipped"
    if po.get("type") != "goods" or not po.get("lines"):
        x.check("3-way match", "pass", f"{po['po']} is a services PO, so 2-way match applies (no goods receipt)"); return "Services PO · 2-way"
    if not x.items:
        x.check("Invoice line items", "warn", "No line items read; cannot match quantities")
        x.flag("LINES_UNREADABLE", "hold", "Goods invoice, but line items could not be read, so quantities can't be checked against the goods receipt.")
        return "No lines to match"

    grns = [g for g in x.master.get("goods_receipts", []) if g["po"] == po["po"]]
    if grns:
        x.check("Goods receipt exists", "pass", ", ".join(f"{g['grn']} ({g['date']})" for g in grns))
    else:
        x.check("Goods receipt exists", "fail", f"No GRN recorded against {po['po']}")
        x.flag("NO_GOODS_RECEIPT", "hold", f"Nothing has been received against {po['po']} yet. Goods must be received before the invoice is paid.")

    def line_key(desc):  # map any description to the closest PO line
        best = max(po["lines"], key=lambda l: similar(desc, l["description"]))
        return best if similar(desc, best["description"]) >= 0.6 else None

    received, billed = {}, {}
    for g in grns:
        for l in g["lines"]:
            k = line_key(l["description"])
            if k: received[k["description"]] = received.get(k["description"], 0) + l["qty"]
    this_no = norm_no(x.f.get("invoice_no"))
    for e in x.ledger:  # quantities already billed on earlier approved/paid invoices against this PO
        if e.get("po") != po["po"] or norm_no(e.get("invoice_no")) == this_no: continue
        for l in e.get("lines") or []:
            k = line_key(l["description"])
            if k: billed[k["description"]] = billed.get(k["description"], 0) + l["qty"]

    tol = x.pol.get("price_tolerance_pct", 1.0)
    for it in x.items:
        pl = line_key(it["description"])
        if not pl:
            x.check(it["description"][:40], "fail", "Not on the purchase order")
            x.flag("LINE_NOT_ON_PO", "hold", f"'{it['description']}' is billed but is not on {po['po']}.")
            continue
        rcv, done = received.get(pl["description"], 0), billed.get(pl["description"], 0)
        available = rcv - done
        price_ok = it["rate"] is not None and it["rate"] <= pl["rate"] * (1 + tol / 100) + 0.005
        qty_ok = it["qty"] <= available
        x.check(it["description"][:40], "pass" if price_ok and qty_ok else "fail",
                f"billed {it['qty']} @ {inr(it['rate'])} · PO {pl['qty']} @ {inr(pl['rate'])} · received {rcv}" + (f", already billed {done}" if done else ""))
        if not qty_ok:
            x.flag("QTY_NOT_RECEIVED", "hold", f"Billed {it['qty']} × {pl['description']}, but only {available} received and not yet billed "
                                               f"({rcv} received{f' − {done} already billed' if done else ''}). Pay only for what has arrived.")
        if not price_ok:
            x.flag("PRICE_ABOVE_PO", "hold", f"{pl['description']} billed at {inr(it['rate'])} vs agreed {inr(pl['rate'])} on the PO "
                                             f"(+{(it['rate'] / pl['rate'] - 1) * 100:.1f}%). Above the {tol}% price tolerance.")
    return "Matched" if worst(x.checks) == "pass" else "Exceptions"


# ---------------------------------------------------------------- 7. duplicate
def stage_duplicate(x):
    if not x.vendor:
        x.check("Not previously processed", "warn", "Skipped: vendor not recognised"); return "Skipped"
    n = norm_no(x.f.get("invoice_no")); dup = None
    for l in x.ledger:
        if l["vendor_id"] != x.vendor["id"]: continue
        if n and norm_no(l["invoice_no"]) == n:
            why = "same invoice number" + (f" (written '{x.f['invoice_no']}' vs '{l['invoice_no']}')" if l["invoice_no"] != x.f.get("invoice_no") else "")
            dup = (l, why); break
        if x.total and abs((l["amount"] or 0) - x.total) < 0.01 and l["po"] == (x.po or {}).get("po"):
            dup = (l, "same amount, PO and vendor"); break
    if dup:
        l, why = dup
        x.check("Not previously processed", "fail", f"Matches {l['invoice_no']} ({l['status']}, {inr(l['amount'])}): {why}")
        x.flag("DUPLICATE", "reject", f"Already {l['status']} as {l['invoice_no']} for {inr(l['amount'])}. Match type: {why}. Paying again would be a double payment.")
        return "Duplicate found"
    x.check("Not previously processed", "pass", f"Checked {sum(1 for l in x.ledger if l['vendor_id'] == x.vendor['id'])} prior invoice(s) from this vendor (fuzzy invoice-no match)")
    return "Unique"


# ---------------------------------------------------------------- 8. decision
ROUTES = [  # first match wins for HOLD
    (("SENDER_MISMATCH", "BANK_MISMATCH"), "Treasury / fraud check"),
    (("FOREIGN_CURRENCY",), "Treasury (FX rate)"),
    (("QTY_NOT_RECEIVED", "NO_GOODS_RECEIPT"), "Stores / receiving team"),
    (("PO_OVERBILLED", "VARIANCE_HIGH", "PRICE_ABOVE_PO", "LINE_NOT_ON_PO"), "Procurement (PO owner)"),
]
EMAIL_CODES = {"PO_MISSING", "MISSING_INVOICE_NO", "MISSING_INVOICE_DATE", "MISSING_TOTAL", "PO_OVERBILLED", "FUTURE_DATED",
               "MATH_LINES", "MATH_TOTAL", "PRICE_ABOVE_PO", "QTY_NOT_RECEIVED", "DUPLICATE", "VENDOR_NOT_APPROVED"}


def decide(x):
    sev = [g["severity"] for g in x.flags]; codes = {g["code"] for g in x.flags}
    decision = "REJECT" if "reject" in sev else "HOLD" if "hold" in sev else "APPROVE"
    blocking = [g for g in x.flags if g["severity"] in ("reject", "hold")]
    route = {"REJECT": "Returned to vendor", "HOLD": "AP reviewer queue", "APPROVE": "Scheduled for payment run"}[decision]
    if decision == "HOLD":
        route = next((r for cs, r in ROUTES if codes & set(cs)), route)
    n = len(blocking)
    headline = {"APPROVE": f"Approved: {inr(x.total)} matches {x.po['po'] if x.po else 'PO'} and all checks passed.",
                "HOLD": f"On hold: {n} issue{'s' if n != 1 else ''} need{'' if n != 1 else 's'} a human decision before payment.",
                "REJECT": "Rejected: this invoice must not be paid as submitted."}[decision]
    email = None
    if x.vendor and codes & EMAIL_CODES:  # never email look-alike senders; always the address on file
        bullets = "\n".join(f"  • {g['message']}" for g in blocking)
        email = {"to": x.vendor["contact"], "subject": f"Invoice {x.f.get('invoice_no') or '(no number)'}: action needed",
                 "body": f"Hi {x.vendor['name']},\n\nThanks for sending invoice {x.f.get('invoice_no') or ''} for {inr(x.total)}. We can't process it yet:\n\n{bullets}\n\n"
                         "Could you reply with a corrected invoice or the missing details? We'll process it the same day.\n\nRegards,\nAccounts Payable, Meridian Retail"}
    for g in x.flags:
        x.check(g["code"], {"reject": "fail", "hold": "warn", "info": "pass"}[g["severity"]], g["message"])
    if not x.flags:
        x.check("All rules passed", "pass", "No exceptions raised")
    x.out["result"] = {"decision": decision, "headline": headline, "route": route, "flags": x.flags, "email": email}
    return decision


PIPELINE = [("ingest", stage_ingest), ("extract", stage_extract), ("validate", stage_validate), ("vendor", stage_vendor),
            ("po", stage_po), ("receipt", stage_receipt), ("duplicate", stage_duplicate), ("decision", decide)]


def run_pipeline(path, filename, master, ledger, emit, source=None):
    x = Ctx(path, filename, master, ledger, emit, source)
    for key, fn in PIPELINE:
        t0 = time.time(); emit(key, {"status": "running"}); time.sleep(PACE)
        x.checks = []
        summary = fn(x)
        status = ({"APPROVE": "pass", "HOLD": "warn", "REJECT": "fail"}[summary] if key == "decision" else worst(x.checks) if x.checks else "pass")
        emit(key, {"status": status, "summary": summary, "checks": x.checks, "ms": int((time.time() - t0) * 1000)})
    return x.out
