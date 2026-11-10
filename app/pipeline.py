"""The decision pipeline. AI/OCR only *reads* the invoice; every decision is a deterministic, auditable rule."""
import os, re, time, difflib, datetime
from .extract import read_pdf, extract

STAGES = [
    ("ingest", "Ingest"), ("extract", "Extract fields"), ("validate", "Validate"),
    ("vendor", "Vendor check"), ("po", "PO match"), ("receipt", "Goods receipt (3-way)"), ("duplicate", "Duplicate check"),
    ("decision", "Decision"),
]
PACE = float(os.environ.get("STAMPD_PACE", "0.45"))  # seconds per stage so the live view is followable on a demo


def norm_no(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def inr(x):
    return "—" if x is None else f"₹{x:,.2f}"


class Run:
    def __init__(self, emit):
        self.emit, self.flags = emit, []

    def stage(self, key, status, summary, checks, t0):
        self.emit(key, {"status": status, "summary": summary, "checks": checks, "ms": int((time.time() - t0) * 1000)})

    def flag(self, code, severity, msg):
        self.flags.append({"code": code, "severity": severity, "message": msg})


def worst(checks):
    s = [c["ok"] for c in checks]
    return "fail" if "fail" in s else "warn" if "warn" in s else "pass"


def run_pipeline(path, filename, master, ledger, emit, source=None):
    R = Run(emit); pol = master["policy"]; out = {"filename": filename}

    # 1. INGEST
    t0 = time.time(); emit("ingest", {"status": "running"}); time.sleep(PACE)
    text, mode, conf, pages = read_pdf(path)
    c = [{"label": "File received", "ok": "pass", "detail": f"{filename} · {pages} page(s)"},
         {"label": "Readable text layer", "ok": "pass" if mode == "text" else "warn",
          "detail": "Native PDF text" if mode == "text" else f"No text layer, scanned image. Ran OCR (confidence {conf:.0%})"}]
    if mode == "ocr" and conf < pol["ocr_review_threshold"]:
        R.flag("LOW_OCR_CONFIDENCE", "hold", f"OCR confidence {conf:.0%} is below the {pol['ocr_review_threshold']:.0%} review threshold.")
    R.stage("ingest", worst(c), "Scanned → OCR" if mode == "ocr" else "Native PDF", c, t0)
    out.update(read_mode=mode, ocr_confidence=conf, raw_text=text)

    # 2. EXTRACT
    t0 = time.time(); emit("extract", {"status": "running"}); time.sleep(PACE)
    f, engine = extract(text)
    out["fields"] = f; out["extractor"] = engine
    keys = [("vendor_name", "Vendor"), ("invoice_no", "Invoice no."), ("invoice_date", "Invoice date"),
            ("po_ref", "PO reference"), ("subtotal", "Subtotal"), ("tax", "Tax"), ("total", "Total"), ("bank_account", "Bank a/c")]
    c = []
    for k, label in keys:
        v = f.get(k)
        shown = inr(v) if k in ("subtotal", "tax", "total") and v is not None else v
        c.append({"label": label, "ok": "pass" if v not in (None, "") else ("warn" if k == "po_ref" else "fail"),
                  "detail": shown if v not in (None, "") else "Not found on invoice"})
    c.append({"label": "Line items", "ok": "pass" if f.get("line_items") else "warn", "detail": f"{len(f.get('line_items') or [])} row(s)"})
    c.append({"label": "Currency", "ok": "pass" if f.get("currency", "INR") == "INR" else "warn", "detail": f.get("currency", "INR")})
    for k in f.get("_derived") or []:
        c.append({"label": f"{k.title()} (derived)", "ok": "warn", "detail": "Not printed; computed from the other two amounts"})
    R.stage("extract", worst(c), f"{sum(1 for x in c if x['ok']=='pass')}/{len(c)} fields · {engine}", c, t0)

    # 3. VALIDATE
    t0 = time.time(); emit("validate", {"status": "running"}); time.sleep(PACE)
    c = []
    for k, label in [("invoice_no", "invoice number"), ("invoice_date", "invoice date"), ("total", "total amount")]:
        if not f.get(k):
            R.flag("MISSING_" + k.upper(), "hold", f"The {label} could not be found on the invoice.")
    c.append({"label": "Mandatory fields present", "ok": "pass" if all(f.get(k) for k in ("invoice_no", "invoice_date", "total")) else "fail",
              "detail": "Invoice no., date, total" if all(f.get(k) for k in ("invoice_no", "invoice_date", "total")) else "Missing mandatory field(s)"})
    items = f.get("line_items") or []
    if items and f.get("subtotal") is not None:
        s = round(sum(i["amount"] for i in items), 2); ok = abs(s - f["subtotal"]) <= pol["math_tolerance_abs"]
        c.append({"label": "Line items add up to subtotal", "ok": "pass" if ok else "fail", "detail": f"{inr(s)} vs {inr(f['subtotal'])}"})
        if not ok: R.flag("MATH_LINES", "hold", f"Line items sum to {inr(s)} but subtotal says {inr(f['subtotal'])}.")
    if None not in (f.get("subtotal"), f.get("tax"), f.get("total")):
        s = round(f["subtotal"] + f["tax"], 2); ok = abs(s - f["total"]) <= pol["math_tolerance_abs"]
        c.append({"label": "Subtotal + tax = total", "ok": "pass" if ok else "fail", "detail": f"{inr(s)} vs {inr(f['total'])}"})
        if not ok: R.flag("MATH_TOTAL", "hold", f"Subtotal + tax = {inr(s)}, but the invoice total is {inr(f['total'])}.")
        rate = f["tax"] / f["subtotal"] * 100 if f["subtotal"] else 0
        c.append({"label": "Tax rate is a valid GST slab", "ok": "pass" if any(abs(rate - r) < 0.05 for r in (0, 5, 12, 18, 28)) else "warn", "detail": f"{rate:.1f}%"})
    iso = f.get("invoice_date_iso")
    if iso:
        d = datetime.date.fromisoformat(iso); today = datetime.date.today(); age = (today - d).days
        if age < -1:
            c.append({"label": "Invoice date is not in the future", "ok": "fail", "detail": f"{iso} is {-age} days ahead"})
            R.flag("FUTURE_DATED", "hold", f"Invoice is dated {iso}, which is in the future. Likely a typo or a pre-billed invoice.")
        elif age > 180:
            c.append({"label": "Invoice is recent", "ok": "warn", "detail": f"{age} days old"})
            R.flag("STALE_INVOICE", "info", f"Invoice is {age} days old. Check it was not already settled offline.")
        else:
            c.append({"label": "Invoice date is plausible", "ok": "pass", "detail": f"{iso} · {age} day(s) ago"})
    elif f.get("invoice_date"):
        c.append({"label": "Invoice date readable", "ok": "warn", "detail": f"Could not interpret '{f['invoice_date']}'"})
    if f.get("currency", "INR") != pol["currency"]:
        c.append({"label": f"Currency is {pol['currency']}", "ok": "fail", "detail": f"Invoice is in {f.get('currency')}"})
        R.flag("FOREIGN_CURRENCY", "hold", f"Invoice is billed in {f.get('currency')}. POs are in {pol['currency']}, so treasury must apply the payment-date FX rate before matching.")
    R.stage("validate", worst(c), "Arithmetic, dates & completeness", c, t0)

    # 4. VENDOR
    t0 = time.time(); emit("vendor", {"status": "running"}); time.sleep(PACE)
    c = []; vendor = None; name = (f.get("vendor_name") or "").lower()
    name_core = re.sub(r"\b(pvt|private|ltd|limited|llp|inc)\b\.?", "", name).strip()
    best = max(master["vendors"], key=lambda v: difflib.SequenceMatcher(None, name_core, v["name"].lower()).ratio())
    score = difflib.SequenceMatcher(None, name_core, best["name"].lower()).ratio()
    if score >= 0.82: vendor = best
    else:  # vendor name not on the first line: look for any known vendor name anywhere in the header
        head = text.lower()[:600]
        for v in master["vendors"]:
            if v["name"].lower() in head:
                vendor, score = v, 1.0; f["vendor_name"] = f.get("vendor_name") or v["name"]; break
    if not vendor:
        c.append({"label": "Vendor in vendor master", "ok": "fail", "detail": f"'{f.get('vendor_name')}' not found"})
        R.flag("VENDOR_UNKNOWN", "reject", f"'{f.get('vendor_name')}' is not in the vendor master.")
    else:
        c.append({"label": "Vendor in vendor master", "ok": "pass", "detail": f"{vendor['name']} ({vendor['id']}) · match {score:.0%}"})
        approved = vendor["status"] == "approved"
        c.append({"label": "Vendor approved for payment", "ok": "pass" if approved else "fail", "detail": vendor["status"].replace("_", " ").upper()})
        if not approved:
            R.flag("VENDOR_NOT_APPROVED", "reject", f"{vendor['name']} is '{vendor['status'].replace('_',' ')}'. Onboarding/KYC must be completed before any payment.")
        else:
            ba = f.get("bank_account")
            if ba and ba != vendor["bank_account"]:
                c.append({"label": "Bank account matches master", "ok": "fail", "detail": f"Invoice ••{ba[-4:]} vs master ••{vendor['bank_account'][-4:]}"})
                R.flag("BANK_MISMATCH", "hold", f"Remit-to account ••{ba[-4:]} differs from the verified account ••{vendor['bank_account'][-4:]}. Classic payment-fraud pattern: verify by phone with a known contact before paying.")
            elif ba:
                c.append({"label": "Bank account matches master", "ok": "pass", "detail": f"••{ba[-4:]} verified"})
            else:
                c.append({"label": "Bank account matches master", "ok": "warn", "detail": "No bank details on invoice; master account will be used"})
    if vendor and source and source.get("from"):
        sender = source["from"].lower(); sdom = sender.split("@")[-1].strip("> ")
        vdom = vendor["contact"].split("@")[-1].lower()
        if sdom == vdom:
            c.append({"label": "Email sender matches vendor domain", "ok": "pass", "detail": f"{sender} · on file: @{vdom}"})
        else:
            sim = difflib.SequenceMatcher(None, sdom, vdom).ratio()
            look = sim >= 0.75
            c.append({"label": "Email sender matches vendor domain", "ok": "fail",
                      "detail": f"{sender} vs @{vdom} on file" + (" (look-alike domain)" if look else "")})
            R.flag("SENDER_MISMATCH", "hold", (f"Sent from @{sdom}, a look-alike of the vendor's real domain @{vdom}. Typical business-email-compromise attack." if look
                    else f"Sent from @{sdom}, but {vendor['name']} bills from @{vdom}.") + " Confirm with the vendor on a known number before paying.")
    out["vendor"] = vendor
    R.stage("vendor", worst(c), vendor["name"] if vendor else "Unknown vendor", c, t0)

    # 5. PO MATCH
    t0 = time.time(); emit("po", {"status": "running"}); time.sleep(PACE)
    c = []; po = None; total = f.get("total")
    pos = {p["po"]: p for p in master["purchase_orders"]}
    if not vendor:
        c.append({"label": "PO lookup", "ok": "warn", "detail": "Skipped: vendor not recognised"})
    elif f.get("po_ref"):
        po = pos.get(f["po_ref"])
        if not po:
            c.append({"label": "PO exists", "ok": "fail", "detail": f"{f['po_ref']} not found"})
            R.flag("PO_NOT_FOUND", "hold", f"PO {f['po_ref']} does not exist in procurement.")
        elif po["vendor_id"] != vendor["id"]:
            c.append({"label": "PO belongs to this vendor", "ok": "fail", "detail": f"{po['po']} is issued to {po['vendor_id']}"})
            R.flag("PO_VENDOR_MISMATCH", "hold", f"{po['po']} was issued to a different vendor."); po = None
        else:
            c.append({"label": "PO exists & belongs to vendor", "ok": "pass", "detail": f"{po['po']} · {po['description']}"})
    else:
        open_pos = [p for p in master["purchase_orders"] if p["vendor_id"] == vendor["id"] and p["status"] == "open"]
        cands = sorted(open_pos, key=lambda p: abs(p["amount"] - (total or 0)))
        if cands and total and abs(cands[0]["amount"] - total) / cands[0]["amount"] * 100 <= pol["variance_tolerance_pct"]:
            po = cands[0]
            c.append({"label": "PO reference on invoice", "ok": "warn", "detail": "None printed"})
            c.append({"label": "Inferred PO", "ok": "warn", "detail": f"{po['po']} ({po['description']}): closest of {len(open_pos)} open PO(s) for this vendor"})
            R.flag("PO_INFERRED", "hold", f"No PO on the invoice. Best match is {po['po']} by vendor + amount. A human should confirm before payment.")
        else:
            c.append({"label": "PO reference on invoice", "ok": "fail", "detail": "None printed, and no open PO matches the amount"})
            R.flag("PO_MISSING", "hold", "No PO reference and no open PO for this vendor matches. Ask the vendor for the PO number.")
    if po and total and f.get("currency", "INR") != pol["currency"]:
        c.append({"label": "Amount vs PO balance", "ok": "warn", "detail": f"Skipped: invoice is in {f.get('currency')}, PO is in {pol['currency']}"})
    elif po and total:
        billed = sum(l["amount"] for l in ledger if l["po"] == po["po"] and l["status"] in ("paid", "approved")
                     and norm_no(l["invoice_no"]) != norm_no(f.get("invoice_no")))
        remaining = round(po["amount"] - billed, 2)
        if billed:
            c.append({"label": "Already billed against PO", "ok": "pass", "detail": f"{inr(billed)} of {inr(po['amount'])} (remaining {inr(remaining)})"})
        var = total - remaining; var_pct = var / remaining * 100 if remaining > 0 else 999
        if var <= 0.005:
            c.append({"label": "Amount within PO balance", "ok": "pass", "detail": f"{inr(total)} ≤ {inr(remaining)}"})
        elif var_pct <= pol["variance_tolerance_pct"] and var <= pol["variance_tolerance_abs"]:
            c.append({"label": "Amount within tolerance", "ok": "warn", "detail": f"Over by {inr(var)} ({var_pct:.2f}%), policy allows ≤{pol['variance_tolerance_pct']}% and ≤{inr(pol['variance_tolerance_abs'])}"})
            R.flag("VARIANCE_OK", "info", f"Invoice is {var_pct:.2f}% ({inr(var)}) over the PO balance, inside the auto-approve tolerance.")
        else:
            code = "PO_OVERBILLED" if billed else "VARIANCE_HIGH"
            c.append({"label": "Amount within PO balance", "ok": "fail", "detail": f"Over by {inr(var)}" + (f" (cumulative billing {inr(billed + total)} vs PO {inr(po['amount'])})" if billed else f" ({var_pct:.1f}%)")})
            R.flag(code, "hold", (f"This is a split invoice. With {inr(billed)} already billed, it brings the PO to {inr(billed + total)}, which is {inr(var)} over the {inr(po['amount'])} PO. A PO amendment or a credit note is needed." if billed
                                  else f"Invoice exceeds the PO by {inr(var)} ({var_pct:.1f}%), beyond tolerance."))
    out["po"] = po
    R.stage("po", worst(c) if c else "pass", po["po"] if po else "No PO matched", c, t0)

    # 6. GOODS RECEIPT (3-way match: invoice line vs PO line vs GRN)
    t0 = time.time(); emit("receipt", {"status": "running"}); time.sleep(PACE)
    c = []
    if not po:
        c.append({"label": "3-way match", "ok": "warn", "detail": "Skipped: no PO matched"}); rsum = "Skipped"
    elif po.get("type") != "goods" or not po.get("lines"):
        c.append({"label": "3-way match", "ok": "pass", "detail": f"{po['po']} is a services PO, so 2-way match applies (no goods receipt)"}); rsum = "Services PO · 2-way"
    elif not items:
        c.append({"label": "Invoice line items", "ok": "warn", "detail": "No line items read; cannot match quantities"}); rsum = "No lines to match"
        R.flag("LINES_UNREADABLE", "hold", "Goods invoice, but line items could not be read, so quantities can't be checked against the goods receipt.")
    else:
        grns = [g for g in master.get("goods_receipts", []) if g["po"] == po["po"]]
        received = {}
        for g in grns:
            for l in g["lines"]:
                received[l["description"].lower()] = received.get(l["description"].lower(), 0) + l["qty"]
        if not grns:
            c.append({"label": "Goods receipt exists", "ok": "fail", "detail": f"No GRN recorded against {po['po']}"})
            R.flag("NO_GOODS_RECEIPT", "hold", f"Nothing has been received against {po['po']} yet. Goods must be received before the invoice is paid.")
        else:
            c.append({"label": "Goods receipt exists", "ok": "pass", "detail": ", ".join(f"{g['grn']} ({g['date']})" for g in grns)})
        tol = pol.get("price_tolerance_pct", 1.0)
        for it in items:
            best_l = max(po["lines"], key=lambda l: difflib.SequenceMatcher(None, it["description"].lower(), l["description"].lower()).ratio())
            if difflib.SequenceMatcher(None, it["description"].lower(), best_l["description"].lower()).ratio() < 0.6:
                c.append({"label": it["description"][:40], "ok": "fail", "detail": "Not on the purchase order"})
                R.flag("LINE_NOT_ON_PO", "hold", f"'{it['description']}' is billed but is not on {po['po']}.")
                continue
            rcv = received.get(best_l["description"].lower(), 0)
            price_ok = it["rate"] is not None and it["rate"] <= best_l["rate"] * (1 + tol / 100) + 0.005
            qty_ok = it["qty"] <= rcv
            ok = "pass" if price_ok and qty_ok else "fail"
            c.append({"label": it["description"][:40], "ok": ok,
                      "detail": f"billed {it['qty']} @ {inr(it['rate'])} · PO {best_l['qty']} @ {inr(best_l['rate'])} · received {rcv}"})
            if not qty_ok:
                R.flag("QTY_NOT_RECEIVED", "hold", f"Billed {it['qty']} × {best_l['description']}, but only {rcv} received (GRN). Pay only for what has arrived.")
            if not price_ok:
                R.flag("PRICE_ABOVE_PO", "hold", f"{best_l['description']} billed at {inr(it['rate'])} vs agreed {inr(best_l['rate'])} on the PO (+{(it['rate']/best_l['rate']-1)*100:.1f}%). Above the {tol}% price tolerance.")
        rsum = "Matched" if worst(c) == "pass" else "Exceptions"
    R.stage("receipt", worst(c), rsum, c, t0)

    # 7. DUPLICATE
    t0 = time.time(); emit("duplicate", {"status": "running"}); time.sleep(PACE)
    c = []; n = norm_no(f.get("invoice_no")); dup = None
    if vendor:
        for l in ledger:
            if l["vendor_id"] != vendor["id"]: continue
            if n and norm_no(l["invoice_no"]) == n:
                dup = (l, "same invoice number" + (f" (written '{f['invoice_no']}' vs '{l['invoice_no']}')" if l["invoice_no"] != f.get("invoice_no") else "")); break
            if total and abs(l["amount"] - total) < 0.01 and l.get("invoice_date") and f.get("invoice_date") and l["po"] == (po or {}).get("po"):
                dup = (l, "same amount, PO and vendor"); break
    if dup:
        l, why = dup
        c.append({"label": "Not previously processed", "ok": "fail", "detail": f"Matches {l['invoice_no']} ({l['status']}, {inr(l['amount'])}): {why}"})
        R.flag("DUPLICATE", "reject", f"Already {l['status']} as {l['invoice_no']} for {inr(l['amount'])}. Match type: {why}. Paying again would be a double payment.")
    else:
        c.append({"label": "Not previously processed", "ok": "pass", "detail": f"Checked {sum(1 for l in ledger if vendor and l['vendor_id']==vendor['id'])} prior invoice(s) from this vendor (fuzzy invoice-no match)"})
    R.stage("duplicate", worst(c), "Duplicate found" if dup else "Unique", c, t0)

    # 8. DECISION
    t0 = time.time(); emit("decision", {"status": "running"}); time.sleep(PACE)
    sev = [x["severity"] for x in R.flags]
    decision = "REJECT" if "reject" in sev else "HOLD" if "hold" in sev else "APPROVE"
    blocking = [x for x in R.flags if x["severity"] in ("reject", "hold")]
    route = {"REJECT": "Returned to vendor", "HOLD": "AP reviewer queue", "APPROVE": "Scheduled for payment run"}[decision]
    if decision == "HOLD" and any(x["code"] == "BANK_MISMATCH" for x in R.flags): route = "Treasury / fraud check"
    if decision == "HOLD" and any(x["code"] in ("PO_OVERBILLED", "VARIANCE_HIGH", "PRICE_ABOVE_PO", "LINE_NOT_ON_PO") for x in R.flags): route = "Procurement (PO owner)"
    if decision == "HOLD" and any(x["code"] in ("QTY_NOT_RECEIVED", "NO_GOODS_RECEIPT") for x in R.flags): route = "Stores / receiving team"
    if decision == "HOLD" and any(x["code"] == "SENDER_MISMATCH" for x in R.flags): route = "Treasury / fraud check"
    if decision == "HOLD" and any(x["code"] == "FOREIGN_CURRENCY" for x in R.flags): route = "Treasury (FX rate)"
    headline = {"APPROVE": f"Approved: {inr(total)} matches {po['po'] if po else 'PO'} and all checks passed.",
                "HOLD": f"On hold: {len(blocking)} issue{'s' if len(blocking) != 1 else ''} need{'' if len(blocking) != 1 else 's'} a human decision before payment.",
                "REJECT": "Rejected: this invoice must not be paid as submitted."}[decision]
    email = None
    if decision == "REJECT" or any(x["code"] in ("PO_MISSING", "MISSING_INVOICE_NO", "MISSING_INVOICE_DATE", "MISSING_TOTAL", "PO_OVERBILLED", "FUTURE_DATED", "MATH_LINES", "MATH_TOTAL", "PRICE_ABOVE_PO", "QTY_NOT_RECEIVED") for x in R.flags):
        to = (vendor or {}).get("contact", "the vendor")
        bullets = "\n".join(f"  • {x['message']}" for x in blocking)
        email = {"to": to, "subject": f"Invoice {f.get('invoice_no') or '(no number)'}: action needed",
                 "body": f"Hi {(vendor or {}).get('name', 'team')},\n\nThanks for sending invoice {f.get('invoice_no') or ''} for {inr(total)}. We can't process it yet:\n\n{bullets}\n\nCould you reply with a corrected invoice or the missing details? We'll process it the same day.\n\nRegards,\nAccounts Payable, Meridian Retail"}
    result = {"decision": decision, "headline": headline, "route": route, "flags": R.flags, "email": email}
    R.stage("decision", {"APPROVE": "pass", "HOLD": "warn", "REJECT": "fail"}[decision], decision,
            [{"label": x["code"], "ok": {"reject": "fail", "hold": "warn", "info": "pass"}[x["severity"]], "detail": x["message"]} for x in R.flags]
            or [{"label": "All rules passed", "ok": "pass", "detail": "No exceptions raised"}], t0)
    out["result"] = result
    return out
