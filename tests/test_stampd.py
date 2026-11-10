"""End-to-end tests: every sample invoice must get the expected decision and reason codes.
Run: STAMPD_PACE=0 pytest -q"""
import json, os, shutil, sys, tempfile, time
import pytest

os.environ.setdefault("STAMPD_PACE", "0")
os.environ.pop("ANTHROPIC_API_KEY", None)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.pipeline import run_pipeline  # noqa: E402
from app.extract import parse_rules, parse_date  # noqa: E402

MASTER = json.load(open(os.path.join(ROOT, "data", "master.json")))
PDFS = os.path.join(ROOT, "samples", "pdfs")
SEED = [dict(l) for l in MASTER["ledger_seed"]]

# sample -> (decision, flags that must be present)
EXPECTED = {
    "01_happy_path_northwind.pdf": ("APPROVE", []),
    "02_within_tolerance_kestrel.pdf": ("APPROVE", ["VARIANCE_OK"]),
    "03_split_po_overbilled_brightline.pdf": ("HOLD", ["PO_OVERBILLED"]),
    "04_duplicate_resubmitted_northwind.pdf": ("REJECT", ["DUPLICATE"]),
    "05_scanned_no_po_apex.pdf": ("HOLD", ["PO_INFERRED"]),
    "06_bank_details_changed_kestrel.pdf": ("HOLD", ["BANK_MISMATCH"]),
    "07_unapproved_vendor_quickfix.pdf": ("REJECT", ["VENDOR_NOT_APPROVED"]),
    "08_different_layout_apex.pdf": ("APPROVE", []),
    "09_math_error_kestrel.pdf": ("HOLD", ["MATH_LINES"]),
    "10_foreign_currency_brightline.pdf": ("HOLD", ["FOREIGN_CURRENCY"]),
    "11_billed_before_delivery_apex.pdf": ("HOLD", ["QTY_NOT_RECEIVED"]),
    "12_price_above_po_apex.pdf": ("HOLD", ["PRICE_ABOVE_PO"]),
}


def run(name, ledger=None, source=None):
    return run_pipeline(os.path.join(PDFS, name), name, MASTER, ledger if ledger is not None else SEED, lambda *a: None, source=source)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_sample_decision(name):
    decision, must = EXPECTED[name]
    out = run(name)
    codes = [f["code"] for f in out["result"]["flags"]]
    assert out["result"]["decision"] == decision, codes
    for m in must:
        assert m in codes


def test_happy_path_extracts_every_field():
    f = run("01_happy_path_northwind.pdf")["fields"]
    assert (f["invoice_no"], f["po_ref"], f["total"], f["tax"], f["subtotal"]) == ("INV-NW-3381", "PO-2026-1041", 118000.0, 18000.0, 100000.0)
    assert len(f["line_items"]) == 2


def test_rerun_after_approval_is_duplicate():
    ledger = SEED + [{"vendor_id": "V001", "invoice_no": "INV-NW-3381", "po": "PO-2026-1041", "amount": 118000.0, "invoice_date": "02-Nov-2026", "status": "approved"}]
    assert run("01_happy_path_northwind.pdf", ledger)["result"]["decision"] == "REJECT"


def test_lookalike_sender_is_held():
    out = run("02_within_tolerance_kestrel.pdf", source={"from": "accounts@kestre1-logistics.in"})
    assert out["result"]["decision"] == "HOLD"
    assert "SENDER_MISMATCH" in [f["code"] for f in out["result"]["flags"]]


def test_genuine_sender_passes():
    out = run("01_happy_path_northwind.pdf", source={"from": "billing@northwind-supplies.in"})
    assert out["result"]["decision"] == "APPROVE"


@pytest.mark.parametrize("raw,iso", [("06-Nov-2026", "2026-11-06"), ("08/11/2026", "2026-11-08"), ("2026-11-02", "2026-11-02"),
                                     ("Nov 6, 2026", "2026-11-06"), ("6 November 2026", "2026-11-06"), ("31/02/2026", None)])
def test_date_parsing(raw, iso):
    assert parse_date(raw) == iso


def test_cgst_sgst_are_summed_and_missing_tax_is_derived():
    f = parse_rules("Acme Ltd\nInvoice No: A-1\nTaxable Amount Rs. 1,000.00\nCGST @ 9% Rs. 90.00\nSGST @ 9% Rs. 90.00\nTotal Amount Rs. 1,180.00")
    assert f["tax"] == 180.0 and f["total"] == 1180.0
    g = parse_rules("Acme Ltd\nInvoice No: A-2\nSubtotal 1,000.00\nGrand Total 1,180.00")
    assert g["tax"] == 180.0 and "tax" in g["_derived"]


# ---------------------------------------------------------------- API
@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient
    import app.main as m
    tmp = tempfile.mkdtemp(); monkeypatch.setattr(m, "DB", os.path.join(tmp, "t.db")); m.init(reset=True)
    yield TestClient(m.app)
    shutil.rmtree(tmp, ignore_errors=True)


def wait(c, rid):
    for _ in range(200):
        d = c.get(f"/api/runs/{rid}").json()
        if d["status"] != "running": return d
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def test_api_review_flow(client):
    rid = client.post("/api/runs", data={"sample": "06_bank_details_changed_kestrel.pdf"}).json()["id"]
    assert wait(client, rid)["decision"] == "HOLD"
    assert client.post(f"/api/runs/{rid}/review", json={"action": "approve", "reason": ""}).status_code == 400
    r = client.post(f"/api/runs/{rid}/review", json={"action": "approve", "reason": "Confirmed by phone on known number", "reviewer": "QA"})
    assert r.status_code == 200 and r.json()["final"] == "APPROVE" and r.json()["review"]["override"]
    assert any(l["invoice_no"] == "KL-5534" for l in client.get("/api/ledger").json())
    assert client.get(f"/api/runs/{rid}/audit").headers["content-disposition"].startswith("attachment")
    assert client.get(f"/api/runs/{rid}/pdf").headers["content-type"] == "application/pdf"


def test_api_duplicate_cannot_be_approved(client):
    rid = client.post("/api/runs", data={"sample": "04_duplicate_resubmitted_northwind.pdf"}).json()["id"]
    assert wait(client, rid)["decision"] == "REJECT"
    assert client.post(f"/api/runs/{rid}/review", json={"action": "approve", "reason": "vendor says it is new"}).status_code == 400


def test_api_email_ingestion(client):
    r = client.post("/api/inbox", data={"sample": "e2_lookalike_domain_kestrel.eml"}).json()
    d = wait(client, r["runs"][0])
    assert d["decision"] == "HOLD" and d["source"]["from"] == "accounts@kestre1-logistics.in"


def test_api_rejects_bad_uploads(client):
    assert client.post("/api/runs", files={"file": ("x.pdf", b"hello", "application/pdf")}).status_code == 400
    assert client.post("/api/runs", files={"file": ("x.txt", b"%PDF-1.4", "text/plain")}).status_code == 400
