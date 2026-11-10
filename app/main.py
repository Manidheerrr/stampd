import os, json, uuid, time, sqlite3, threading, shutil, traceback
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from .pipeline import run_pipeline, STAGES

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
        c.execute("INSERT INTO runs (id,created,filename,status,stages) VALUES (?,?,?,?,?)", (rid, time.time(), filename, "running", json.dumps(stages)))
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
    if not data.startswith(b"%PDF"): raise HTTPException(400, "That file is not a valid PDF")
    dst = os.path.join(UP, uuid.uuid4().hex[:6] + "_" + os.path.basename(file.filename))
    open(dst, "wb").write(data)
    return start(dst, file.filename)


def row(r, full=False):
    d = dict(r); d["stages"] = json.loads(d["stages"] or "[]")
    out = json.loads(d.pop("output") or "null")
    if full: d["output"] = out
    elif out: d["route"] = out["result"]["route"]; d["flags"] = [x["code"] for x in out["result"]["flags"] if x["severity"] != "info"]
    return d


@app.get("/api/runs")
def list_runs():
    with LOCK, db() as c:
        return [row(r) for r in c.execute("SELECT * FROM runs ORDER BY created DESC LIMIT 200")]


@app.get("/api/runs/{rid}")
def get_run(rid: str):
    with LOCK, db() as c:
        r = c.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
    if not r: raise HTTPException(404)
    return row(r, full=True)


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
