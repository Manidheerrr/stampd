import os, json, uuid, time, sqlite3, threading, shutil, traceback
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Body
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from .pipeline import run_pipeline, STAGES
from .extract import extractor_name

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "stampd.db")
UP = os.path.join(ROOT, "data", "uploads"); os.makedirs(UP, exist_ok=True)
SAMPLES = os.path.join(ROOT, "samples", "pdfs")
MASTER = json.load(open(os.path.join(ROOT, "data", "master.json")))
LOCK = threading.Lock()
app = FastAPI(title="Stampd")


def db():
    c = sqlite3.connect(DB, check_same_thread=False); c.row_factory = sqlite3.Row; return c


def init(reset=False):
    with LOCK, db() as c:
        if reset:
            c.execute("DROP TABLE IF EXISTS runs"); c.execute("DROP TABLE IF EXISTS ledger")
        c.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, created REAL, filename TEXT, status TEXT, decision TEXT, vendor TEXT, invoice_no TEXT, total REAL, duration REAL, stages TEXT, output TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, vendor_id TEXT, invoice_no TEXT, po TEXT, amount REAL, invoice_date TEXT, status TEXT, run_id TEXT)")
        for col in ("path TEXT", "review TEXT"):
            try: c.execute("ALTER TABLE runs ADD COLUMN " + col)
            except sqlite3.OperationalError: pass
        if not c.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]:
            for l in MASTER["ledger_seed"]:
                c.execute("INSERT INTO ledger (vendor_id,invoice_no,po,amount,invoice_date,status) VALUES (?,?,?,?,?,?)",
                          (l["vendor_id"], l["invoice_no"], l["po"], l["amount"], l["invoice_date"], l["status"]))


init()


def execute(run_id, path, filename):
    stages = {k: {"key": k, "label": lbl, "status": "pending"} for k, lbl in STAGES}

    def emit(key, data):
        stages[key].update(data)
        with LOCK, db() as c:
            c.execute("UPDATE runs SET stages=? WHERE id=?", (json.dumps(list(stages.values())), run_id))

    t0 = time.time()
    try:
        with LOCK, db() as c:
            ledger = [dict(r) for r in c.execute("SELECT * FROM ledger")]
        out = run_pipeline(path, filename, MASTER, ledger, emit)
        f, res = out["fields"], out["result"]
        with LOCK, db() as c:
            c.execute("UPDATE runs SET status='done', decision=?, vendor=?, invoice_no=?, total=?, duration=?, output=? WHERE id=?",
                      (res["decision"], (out.get("vendor") or {}).get("name") or f.get("vendor_name"), f.get("invoice_no"), f.get("total"),
                       round(time.time() - t0, 2), json.dumps(out, default=str), run_id))
            if res["decision"] == "APPROVE" and out.get("vendor"):
                c.execute("INSERT INTO ledger (vendor_id,invoice_no,po,amount,invoice_date,status,run_id) VALUES (?,?,?,?,?,?,?)",
                          (out["vendor"]["id"], f.get("invoice_no"), (out.get("po") or {}).get("po"), f.get("total"), f.get("invoice_date"), "approved", run_id))
    except Exception as e:
        traceback.print_exc()
        for s in stages.values():
            if s["status"] == "running":
                s.update(status="fail", summary="Error", checks=[{"label": "Processing error", "ok": "fail", "detail": str(e)[:300]}])
        with LOCK, db() as c:
            c.execute("UPDATE runs SET status='error', decision='ERROR', duration=?, stages=? WHERE id=?",
                      (round(time.time() - t0, 2), json.dumps(list(stages.values())), run_id))


def start(path, filename):
    rid = uuid.uuid4().hex[:8]
    stages = [{"key": k, "label": l, "status": "pending"} for k, l in STAGES]
    with LOCK, db() as c:
        c.execute("INSERT INTO runs (id,created,filename,status,stages,path) VALUES (?,?,?,?,?,?)", (rid, time.time(), filename, "running", json.dumps(stages), path))
    threading.Thread(target=execute, args=(rid, path, filename), daemon=True).start()
    return {"id": rid}


@app.post("/api/runs")
async def create_run(file: UploadFile = File(None), sample: str = Form(None)):
    if sample:
        src = os.path.join(SAMPLES, os.path.basename(sample))
        if not os.path.exists(src): raise HTTPException(404, "Sample not found")
        dst = os.path.join(UP, uuid.uuid4().hex[:6] + "_" + os.path.basename(sample)); shutil.copy(src, dst)
        return start(dst, os.path.basename(sample))
    if not file: raise HTTPException(400, "Upload a PDF or choose a sample")
    if not file.filename.lower().endswith(".pdf"): raise HTTPException(400, "Only PDF invoices are supported")
    data = await file.read()
    if len(data) > 15 * 1024 * 1024: raise HTTPException(400, "File is larger than 15 MB")
    if not data.startswith(b"%PDF"): raise HTTPException(400, "That file is not a valid PDF")
    dst = os.path.join(UP, uuid.uuid4().hex[:6] + "_" + os.path.basename(file.filename))
    open(dst, "wb").write(data)
    return start(dst, file.filename)


FINAL = {"approve": "APPROVE", "reject": "REJECT"}


def row(r, full=False):
    d = dict(r); d["stages"] = json.loads(d["stages"] or "[]"); d.pop("path", None)
    d["review"] = json.loads(d.get("review") or "null")
    d["final"] = FINAL[d["review"]["action"]] if d["review"] else d["decision"]
    out = json.loads(d.pop("output") or "null")
    if full: d["output"] = out
    elif out: d["route"] = out["result"]["route"]; d["flags"] = [x["code"] for x in out["result"]["flags"] if x["severity"] != "info"]
    return d


def fetch(rid):
    with LOCK, db() as c:
        r = c.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
    if not r: raise HTTPException(404, "Run not found")
    return r


@app.get("/api/runs/{rid}/pdf")
def run_pdf(rid: str):
    p = fetch(rid)["path"]
    if not p or not os.path.exists(p): raise HTTPException(404, "Source PDF no longer available")
    return FileResponse(p, media_type="application/pdf")


@app.get("/api/runs/{rid}/audit")
def run_audit(rid: str):
    d = row(fetch(rid), full=True)
    if d.get("output"): d["output"].pop("raw_text", None)
    body = {"generated_by": "Stampd", "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "policy": MASTER["policy"], "run": d}
    return Response(json.dumps(body, indent=2, default=str), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="stampd-audit-{rid}.json"'})


@app.post("/api/runs/{rid}/review")
def review(rid: str, payload: dict = Body(...)):
    action = payload.get("action"); reason = (payload.get("reason") or "").strip(); reviewer = (payload.get("reviewer") or "AP reviewer").strip()[:60]
    if action not in FINAL: raise HTTPException(400, "Action must be approve or reject")
    r = fetch(rid)
    if r["status"] != "done": raise HTTPException(400, "Run has not finished")
    out = json.loads(r["output"])
    overriding = (action == "approve" and r["decision"] != "APPROVE") or (action == "reject" and r["decision"] == "APPROVE")
    if overriding and len(reason) < 8: raise HTTPException(400, "Overriding the system decision needs a reason (8+ characters)")
    if action == "approve" and any(x["code"] == "DUPLICATE" for x in out["result"]["flags"]):
        raise HTTPException(400, "Duplicates cannot be approved: this would be a double payment")
    rev = {"action": action, "reason": reason, "reviewer": reviewer, "at": time.time(), "override": overriding, "system_decision": r["decision"]}
    with LOCK, db() as c:
        c.execute("UPDATE runs SET review=? WHERE id=?", (json.dumps(rev), rid))
        c.execute("DELETE FROM ledger WHERE run_id=?", (rid,))
        v, f = out.get("vendor"), out["fields"]
        if action == "approve" and v:
            c.execute("INSERT INTO ledger (vendor_id,invoice_no,po,amount,invoice_date,status,run_id) VALUES (?,?,?,?,?,?,?)",
                      (v["id"], f.get("invoice_no"), (out.get("po") or {}).get("po"), f.get("total"), f.get("invoice_date"), "approved", rid))
    return row(fetch(rid), full=True)


@app.get("/api/config")
def config():
    return {"extractor": extractor_name(), "policy": MASTER["policy"], "vendors": len(MASTER["vendors"]), "pos": len(MASTER["purchase_orders"])}


@app.get("/api/runs")
def list_runs():
    with LOCK, db() as c:
        return [row(r) for r in c.execute("SELECT * FROM runs ORDER BY created DESC LIMIT 200")]


@app.get("/api/runs/{rid}")
def get_run(rid: str):
    return row(fetch(rid), full=True)


@app.get("/api/samples")
def samples():
    meta = {
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
    }
    out = []
    for fn in sorted(os.listdir(SAMPLES)):
        if fn.endswith(".pdf"):
            t, d, k = meta.get(fn[:2], (fn, "", "edge")); out.append({"file": fn, "title": t, "desc": d, "kind": k})
    return out


@app.get("/api/samples/{name}")
def sample_pdf(name: str):
    p = os.path.join(SAMPLES, os.path.basename(name))
    if not os.path.exists(p): raise HTTPException(404)
    return FileResponse(p, media_type="application/pdf")


@app.get("/api/master")
def master():
    return MASTER


@app.get("/api/ledger")
def ledger():
    with LOCK, db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM ledger ORDER BY id DESC")]


@app.post("/api/reset")
def reset():
    init(reset=True); return {"ok": True}


app.mount("/", StaticFiles(directory=os.path.join(ROOT, "static"), html=True), name="static")
