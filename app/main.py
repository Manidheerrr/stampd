"""Stampd API: runs, reviewer actions, email intake, per-visitor sandboxes.

Every browser gets its own sandbox (X-Sandbox header, or ?sb= on links), so visitors never see or reset each
other's runs. The seeded payment history in data/master.json is shared and read-only; approvals, test
vendors and test POs live inside the visitor's sandbox.
"""
import os, re, json, uuid, time, copy, sqlite3, threading, shutil, traceback, email, email.policy, email.utils
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Body, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from .pipeline import run_pipeline, STAGES, norm_no
from .extract import extractor_name

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "stampd_v2.db")
UP = os.path.join(ROOT, "data", "uploads"); os.makedirs(UP, exist_ok=True)
SAMPLES = os.path.join(ROOT, "samples", "pdfs")
EMAILS = os.path.join(ROOT, "samples", "emails")
MASTER = json.load(open(os.path.join(ROOT, "data", "master.json")))
LOCK = threading.Lock()
FINAL = {"approve": "APPROVE", "reject": "REJECT"}
app = FastAPI(title="Stampd")

SAMPLE_META = {
    "01": ("Happy path", "Clean PDF, exact PO match", "happy"),
    "02": ("Within tolerance", "1.4% over PO, still inside policy", "happy"),
    "03": ("Split PO, over-billed", "2nd invoice pushes PO past its value", "edge"),
    "04": ("Disguised duplicate", "Resent as INV/NW/3290", "edge"),
    "05": ("Scanned, no PO", "OCR + PO inferred from amount", "edge"),
    "06": ("Bank details changed", "New remit-to account, fraud pattern", "edge"),
    "07": ("Unapproved vendor", "Vendor still in KYC", "edge"),
    "08": ("Different layout", "CGST+SGST split, Rs., HSN column", "happy"),
    "09": ("Arithmetic error", "Line items don't add up to subtotal", "edge"),
    "10": ("Foreign currency", "Billed in USD against an INR PO", "edge"),
    "11": ("Billed before delivery", "Invoice qty exceeds goods received", "edge"),
    "12": ("Price above PO", "Unit price 10% over the agreed rate", "edge"),
}


# ---------------------------------------------------------------- storage
def db():
    c = sqlite3.connect(DB, check_same_thread=False); c.row_factory = sqlite3.Row; return c


def init(reset=False):
    with LOCK, db() as c:
        if reset:
            for t in ("runs", "ledger", "extra"): c.execute(f"DROP TABLE IF EXISTS {t}")
        c.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, sandbox TEXT, created REAL, filename TEXT, status TEXT, decision TEXT,"
                  " vendor TEXT, invoice_no TEXT, total REAL, duration REAL, stages TEXT, output TEXT, path TEXT, review TEXT, source TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, sandbox TEXT, vendor_id TEXT, invoice_no TEXT, po TEXT,"
                  " amount REAL, invoice_date TEXT, status TEXT, run_id TEXT, lines TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS extra (id INTEGER PRIMARY KEY AUTOINCREMENT, sandbox TEXT, kind TEXT, data TEXT)")
        c.execute("CREATE INDEX IF NOT EXISTS runs_sb ON runs(sandbox, created)")


init()


def sandbox_of(request: Request):
    sb = request.headers.get("x-sandbox") or request.query_params.get("sb") or "public"
    return sb if re.fullmatch(r"[A-Za-z0-9_-]{4,40}", sb) else "public"


def ledger_for(sb):
    seed = [dict(l, lines=l.get("lines") or [], run_id=None) for l in MASTER["ledger_seed"]]
    with LOCK, db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM ledger WHERE sandbox=?", (sb,))]
    for r in rows: r["lines"] = json.loads(r["lines"] or "[]")
    return seed + rows


def master_for(sb):
    m = copy.deepcopy(MASTER)
    with LOCK, db() as c:
        for r in c.execute("SELECT kind, data FROM extra WHERE sandbox=? ORDER BY id", (sb,)):
            d = json.loads(r["data"])
            (m["vendors"] if r["kind"] == "vendor" else m["purchase_orders"]).append(d)
    return m


def add_to_ledger(c, sb, out, rid):
    v, f = out.get("vendor"), out["fields"]
    if not v: return
    lines = [{"description": i["description"], "qty": i["qty"]} for i in (f.get("line_items") or [])]
    c.execute("INSERT INTO ledger (sandbox,vendor_id,invoice_no,po,amount,invoice_date,status,run_id,lines) VALUES (?,?,?,?,?,?,?,?,?)",
              (sb, v["id"], f.get("invoice_no"), (out.get("po") or {}).get("po"), f.get("total"), f.get("invoice_date"), "approved", rid, json.dumps(lines)))


# ---------------------------------------------------------------- running
def execute(rid, sb, path, filename, source=None):
    stages = {k: {"key": k, "label": lbl, "status": "pending"} for k, lbl in STAGES}

    def emit(key, data):
        stages[key].update(data)
        with LOCK, db() as c:
            c.execute("UPDATE runs SET stages=? WHERE id=?", (json.dumps(list(stages.values())), rid))

    t0 = time.time()
    try:
        out = run_pipeline(path, filename, master_for(sb), ledger_for(sb), emit, source=source)
        f, res = out["fields"], out["result"]
        with LOCK, db() as c:
            c.execute("UPDATE runs SET status='done', decision=?, vendor=?, invoice_no=?, total=?, duration=?, output=? WHERE id=?",
                      (res["decision"], (out.get("vendor") or {}).get("name") or f.get("vendor_name"), f.get("invoice_no"), f.get("total"),
                       round(time.time() - t0, 2), json.dumps(out, default=str), rid))
            if res["decision"] == "APPROVE": add_to_ledger(c, sb, out, rid)
    except Exception as e:
        traceback.print_exc()
        for s in stages.values():
            if s["status"] == "running":
                s.update(status="fail", summary="Error", checks=[{"label": "Processing error", "ok": "fail", "detail": str(e)[:300]}])
        with LOCK, db() as c:
            c.execute("UPDATE runs SET status='error', decision='ERROR', duration=?, stages=? WHERE id=?",
                      (round(time.time() - t0, 2), json.dumps(list(stages.values())), rid))


def start(sb, path, filename, source=None):
    rid = uuid.uuid4().hex[:8]
    stages = [{"key": k, "label": l, "status": "pending"} for k, l in STAGES]
    with LOCK, db() as c:
        c.execute("INSERT INTO runs (id,sandbox,created,filename,status,stages,path,source) VALUES (?,?,?,?,?,?,?,?)",
                  (rid, sb, time.time(), filename, "running", json.dumps(stages), path, json.dumps(source) if source else None))
    threading.Thread(target=execute, args=(rid, sb, path, filename, source), daemon=True).start()
    return {"id": rid}


def row(r, full=False):
    d = dict(r); d["stages"] = json.loads(d["stages"] or "[]"); d.pop("path", None); d.pop("sandbox", None)
    d["review"] = json.loads(d.get("review") or "null"); d["source"] = json.loads(d.get("source") or "null")
    d["final"] = FINAL[d["review"]["action"]] if d["review"] else d["decision"]
    out = json.loads(d.pop("output") or "null")
    if full: d["output"] = out
    elif out: d["route"] = out["result"]["route"]; d["flags"] = [x["code"] for x in out["result"]["flags"] if x["severity"] != "info"]
    return d


def fetch(rid, sb):
    with LOCK, db() as c:
        r = c.execute("SELECT * FROM runs WHERE id=? AND sandbox=?", (rid, sb)).fetchone()
    if not r: raise HTTPException(404, "Run not found in your sandbox")
    return r


# ---------------------------------------------------------------- endpoints
@app.post("/api/runs")
async def create_run(request: Request, file: UploadFile = File(None), sample: str = Form(None)):
    sb = sandbox_of(request)
    if sample:
        src = os.path.join(SAMPLES, os.path.basename(sample))
        if not os.path.exists(src): raise HTTPException(404, "Sample not found")
        dst = os.path.join(UP, uuid.uuid4().hex[:6] + "_" + os.path.basename(sample)); shutil.copy(src, dst)
        return start(sb, dst, os.path.basename(sample))
    if not file: raise HTTPException(400, "Upload a PDF or choose a sample")
    if not file.filename.lower().endswith(".pdf"): raise HTTPException(400, "Only PDF invoices are supported")
    data = await file.read()
    if len(data) > 15 * 1024 * 1024: raise HTTPException(400, "File is larger than 15 MB")
    if not data.startswith(b"%PDF"): raise HTTPException(400, "That file is not a valid PDF")
    dst = os.path.join(UP, uuid.uuid4().hex[:6] + "_" + os.path.basename(file.filename))
    open(dst, "wb").write(data)
    return start(sb, dst, file.filename)


@app.get("/api/runs")
def list_runs(request: Request):
    with LOCK, db() as c:
        return [row(r) for r in c.execute("SELECT * FROM runs WHERE sandbox=? ORDER BY created DESC LIMIT 200", (sandbox_of(request),))]


@app.get("/api/runs/{rid}")
def get_run(rid: str, request: Request):
    return row(fetch(rid, sandbox_of(request)), full=True)


@app.get("/api/runs/{rid}/pdf")
def run_pdf(rid: str, request: Request):
    p = fetch(rid, sandbox_of(request))["path"]
    if not p or not os.path.exists(p): raise HTTPException(404, "Source PDF no longer available")
    return FileResponse(p, media_type="application/pdf")


@app.get("/api/runs/{rid}/audit")
def run_audit(rid: str, request: Request):
    d = row(fetch(rid, sandbox_of(request)), full=True)
    if d.get("output"): d["output"].pop("raw_text", None)
    body = {"generated_by": "Stampd", "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "policy": MASTER["policy"], "run": d}
    return Response(json.dumps(body, indent=2, default=str), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="stampd-audit-{rid}.json"'})


@app.post("/api/runs/{rid}/review")
def review(rid: str, request: Request, payload: dict = Body(...)):
    sb = sandbox_of(request)
    action = payload.get("action"); reason = (payload.get("reason") or "").strip(); reviewer = (payload.get("reviewer") or "AP reviewer").strip()[:60]
    if action not in FINAL: raise HTTPException(400, "Action must be approve or reject")
    r = fetch(rid, sb)
    if r["status"] != "done": raise HTTPException(400, "Run has not finished")
    out = json.loads(r["output"]); f, v, po = out["fields"], out.get("vendor"), out.get("po")
    overriding = (action == "approve" and r["decision"] != "APPROVE") or (action == "reject" and r["decision"] == "APPROVE")
    if overriding and len(reason) < 8: raise HTTPException(400, "Overruling Stampd needs a reason (at least 8 characters)")
    if action == "approve":
        if any(x["code"] == "DUPLICATE" for x in out["result"]["flags"]):
            raise HTTPException(400, "Duplicates cannot be approved: this would be a double payment")
        if not v: raise HTTPException(400, "Unknown vendors cannot be paid. Add the vendor first.")
        # Re-check against the ledger *now*: another copy may have been approved since this run finished.
        others = [l for l in ledger_for(sb) if l.get("run_id") != rid]
        n = norm_no(f.get("invoice_no"))
        for l in others:
            if l["vendor_id"] == v["id"] and n and norm_no(l["invoice_no"]) == n:
                raise HTTPException(400, f"Invoice {f.get('invoice_no')} was already {l['status']} (as {l['invoice_no']}) after this run. Approving again would pay it twice.")
        if po and f.get("total"):
            billed = sum(l["amount"] or 0 for l in others if l["po"] == po["po"])
            if billed + f["total"] > po["amount"] * 1.02 and len(reason) < 8:
                raise HTTPException(400, f"Approving this takes {po['po']} to ₹{billed + f['total']:,.0f} against a ₹{po['amount']:,.0f} PO. Give a reason to override.")
    rev = {"action": action, "reason": reason, "reviewer": reviewer, "at": time.time(), "override": overriding, "system_decision": r["decision"]}
    with LOCK, db() as c:
        c.execute("UPDATE runs SET review=? WHERE id=?", (json.dumps(rev), rid))
        c.execute("DELETE FROM ledger WHERE run_id=?", (rid,))
        if action == "approve": add_to_ledger(c, sb, out, rid)
    return row(fetch(rid, sb), full=True)


@app.post("/api/runs/{rid}/onboard")
def onboard(rid: str, request: Request, payload: dict = Body(default={})):
    """Sandbox only: add the invoice's vendor (and optionally a matching PO) as test master data, then re-run."""
    sb = sandbox_of(request)
    r = fetch(rid, sb)
    if r["status"] != "done": raise HTTPException(400, "Run has not finished")
    out = json.loads(r["output"]); f = out["fields"]
    if out.get("vendor"): raise HTTPException(400, "This vendor is already in the vendor master")
    tag = uuid.uuid4().hex[:4].upper()
    name = (payload.get("name") or f.get("vendor_name") or "Test vendor").strip()[:80]
    sender = (json.loads(r["source"]) or {}).get("from") if r["source"] else None
    vendor = {"id": "T-" + tag, "name": name, "status": "approved", "bank_account": f.get("bank_account") or "",
              "ifsc": f.get("ifsc") or "", "contact": sender or f"accounts@{re.sub('[^a-z0-9]', '', name.lower())[:20] or 'vendor'}.example", "sandbox": True}
    existing = {p["po"] for p in master_for(sb)["purchase_orders"]}
    with LOCK, db() as c:
        c.execute("INSERT INTO extra (sandbox,kind,data) VALUES (?,?,?)", (sb, "vendor", json.dumps(vendor)))
        if payload.get("create_po", True) and f.get("total"):
            po_no = f.get("po_ref") if f.get("po_ref") and f["po_ref"] not in existing else f"PO-TEST-{tag}"
            po = {"po": po_no, "vendor_id": vendor["id"], "amount": f["total"], "status": "open",
                  "description": "Test PO created in sandbox", "type": "services", "sandbox": True}
            c.execute("INSERT INTO extra (sandbox,kind,data) VALUES (?,?,?)", (sb, "po", json.dumps(po)))
    dst = os.path.join(UP, uuid.uuid4().hex[:6] + "_" + r["filename"]); shutil.copy(r["path"], dst)
    return start(sb, dst, r["filename"], json.loads(r["source"]) if r["source"] else None)


@app.get("/api/config")
def config(request: Request):
    m = master_for(sandbox_of(request))
    return {"extractor": extractor_name(), "policy": MASTER["policy"], "vendors": len(m["vendors"]), "pos": len(m["purchase_orders"])}


@app.get("/api/master")
def master(request: Request):
    return master_for(sandbox_of(request))


@app.get("/api/ledger")
def ledger(request: Request):
    return ledger_for(sandbox_of(request))


@app.post("/api/reset")
def reset(request: Request):
    sb = sandbox_of(request)
    with LOCK, db() as c:
        for t in ("runs", "ledger", "extra"): c.execute(f"DELETE FROM {t} WHERE sandbox=?", (sb,))
    return {"ok": True}


@app.get("/api/samples")
def samples():
    out = []
    for fn in sorted(os.listdir(SAMPLES)):
        if fn.endswith(".pdf"):
            t, d, k = SAMPLE_META.get(fn[:2], (fn, "", "edge")); out.append({"file": fn, "title": t, "desc": d, "kind": k})
    return out


@app.get("/api/samples/{name}")
def sample_pdf(name: str):
    p = os.path.join(SAMPLES, os.path.basename(name))
    if not os.path.exists(p): raise HTTPException(404)
    return FileResponse(p, media_type="application/pdf")


# ---------------------------------------------------------------- email intake
def parse_eml(raw):
    m = email.message_from_bytes(raw, policy=email.policy.default)
    atts = [(p.get_filename() or "invoice.pdf", p.get_content()) for p in m.iter_attachments()
            if (p.get_content_type() == "application/pdf" or (p.get_filename() or "").lower().endswith(".pdf"))]
    name, addr = email.utils.parseaddr(str(m.get("From", "")))
    body = m.get_body(preferencelist=("plain",))
    return {"from": addr, "from_name": name, "subject": str(m.get("Subject", "")), "date": str(m.get("Date", "")),
            "body": (body.get_content() if body else "")[:600]}, atts


@app.get("/api/inbox")
def inbox():
    out = []
    for fn in sorted(os.listdir(EMAILS)) if os.path.isdir(EMAILS) else []:
        if fn.endswith(".eml"):
            meta, atts = parse_eml(open(os.path.join(EMAILS, fn), "rb").read())
            out.append({"file": fn, **meta, "attachments": [a[0] for a in atts]})
    return out


@app.post("/api/inbox")
async def ingest_email(request: Request, file: UploadFile = File(None), sample: str = Form(None)):
    sb = sandbox_of(request)
    if sample:
        src = os.path.join(EMAILS, os.path.basename(sample))
        if not os.path.exists(src): raise HTTPException(404, "Sample email not found")
        raw = open(src, "rb").read()
    elif file:
        raw = await file.read()
        if len(raw) > 20 * 1024 * 1024: raise HTTPException(400, "Email is larger than 20 MB")
    else:
        raise HTTPException(400, "Upload an .eml file or choose a sample")
    try:
        meta, atts = parse_eml(raw)
    except Exception:
        raise HTTPException(400, "That file is not a valid email (.eml)")
    if not atts: raise HTTPException(400, "The email has no PDF attachment")
    meta["channel"] = "email"
    runs = []
    for fn, data in atts[:5]:
        dst = os.path.join(UP, uuid.uuid4().hex[:6] + "_" + os.path.basename(fn))
        open(dst, "wb").write(data)
        runs.append(start(sb, dst, os.path.basename(fn), source=meta)["id"])
    return {"runs": runs, "from": meta["from"], "subject": meta["subject"]}


app.mount("/", StaticFiles(directory=os.path.join(ROOT, "static"), html=True), name="static")
