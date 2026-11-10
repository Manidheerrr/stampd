# Stampd: invoice processing, from PDF to decision

**Zamp ASA case study · PS-1 (Finance / AP)**

Stampd takes a vendor invoice PDF (native or scanned) and produces a decision of **APPROVE**, **HOLD** or **REJECT**. Every decision comes with reason codes, a routing destination, a drafted vendor email where one is needed, and a full audit trail of each stage.

## Run it

```bash
# needs Python 3.10+, plus tesseract and poppler for scanned invoices
#   macOS:   brew install tesseract poppler
#   Ubuntu:  sudo apt install tesseract-ocr poppler-utils
./run.sh            # → http://localhost:8000
```

**Docker:** `docker build -t stampd . && docker run -p 8000:8000 stampd`

**Deploy (live link):** push this folder to GitHub → Render.com → *New Web Service* → it detects the `Dockerfile` → deploy. On the free tier it sleeps when idle, so open the link 1 minute before your demo.

## Pipeline

| # | Stage | What happens |
|---|---|---|
| 1 | Ingest | Detects whether the PDF has a text layer. If not, it's a scan, so the stage rasterises it and runs OCR, recording a confidence score. |
| 2 | Extract | Pulls vendor, invoice no., date, PO ref, subtotal, tax, total, bank a/c, IFSC and line items. Handles label variants such as *Invoice No / Bill # / Invoice Number* and *Grand Total / Amount Due / Total Payable*. |
| 3 | Validate | Checks that the mandatory fields are present, that line items sum to the subtotal, that subtotal + tax = total (±₹1), and that the tax rate is a valid GST slab. |
| 4 | Vendor | Fuzzy-matches the vendor against the vendor master, checks the vendor is approved for payment, and checks the **bank account matches the verified one**. |
| 5 | PO match | Matches an explicit PO, or **infers** one from vendor + amount when none is printed. Checks the invoice against the **remaining** PO balance, which covers split invoices, and applies tolerance. |
| 6 | Duplicate | Matches normalised invoice numbers (`INV/NW/3290` = `INV-NW-3290`) and same vendor + amount + PO against the payment ledger. |
| 7 | Decision | Combines all flags into a decision with reason codes, a routing destination and a vendor email draft. |

**Key design choice:** OCR and the parser only *read* the invoice. **Every decision is a deterministic rule**, so the same invoice always gets the same answer and every answer can be explained to an auditor. AI is used where reading is fuzzy, never where a payment is decided.

## Decision rules

| Severity | Rule | Routed to |
|---|---|---|
| REJECT | Vendor not in master / not approved (KYC pending) | Back to vendor |
| REJECT | Duplicate of a paid or approved invoice | Back to vendor |
| HOLD | Bank account differs from the vendor master | Treasury / fraud check |
| HOLD | Cumulative billing exceeds PO (split invoices) / variance beyond tolerance | Procurement (PO owner) |
| HOLD | PO inferred rather than printed · PO missing · PO belongs to another vendor | AP reviewer |
| HOLD | Missing invoice no./date/total · arithmetic mismatch · OCR confidence < 80% | AP reviewer |
| info | Over PO by ≤2% **and** ≤₹10,000 | Auto-approve, variance noted |

Policy values live in `data/master.json` (`policy`).

## Test deck (`samples/pdfs/`)

| File | Scenario | Expected |
|---|---|---|
| 01 | Happy path: clean PDF, exact PO match | APPROVE |
| 02 | 1.4% over PO, inside tolerance | APPROVE (variance noted) |
| 03 | **Split PO**: 2nd invoice pushes cumulative billing ₹35,400 over PO | HOLD → Procurement |
| 04 | **Disguised duplicate**: resent as `INV/NW/3290` | REJECT |
| 05 | **Scanned image, no PO**: OCR + PO inferred | HOLD → AP review |
| 06 | **Bank details changed**: new remit-to account | HOLD → Treasury |
| 07 | Vendor still in KYC | REJECT |

Re-running 01 after it's approved is caught as a duplicate, because approved invoices are written to the ledger. **Reset demo** (top right) restores the seeded ledger.

## Assumptions (to reference in the interview)
- Invoices arrive one at a time as PDFs. Email ingestion is a front-end to this pipeline and is out of scope.
- The PO dataset, vendor master and payment ledger are JSON/SQLite stand-ins for an ERP. Amounts are tax-inclusive INR.
- Tolerance is a dual cap (2% **and** ₹10k), so a 2% variance on a ₹50L invoice still goes to a human.
- When a PO is inferred, the invoice is *always* held. A plausible guess must never auto-pay.
- A bank-detail change is always held, even if everything else is perfect. This is the most common real-world AP fraud.

## Optional: Claude-based extraction
The rules parser covers the invoice formats in the test deck. For arbitrary vendor layouts, switch extraction to Claude:
```bash
export ANTHROPIC_API_KEY=sk-ant-...   EXTRACTOR=llm
```
If the LLM call fails, the process falls back to the rules parser. *This path was not tested in the build environment because no API key was available. Verify it before demoing it.*

## Project layout
```
app/main.py       API, run store (SQLite), background execution
app/pipeline.py   7-stage decision pipeline and rules
app/extract.py    PDF text / OCR + field extraction
data/master.json  vendors, POs, policy, seeded payment ledger
samples/          invoice generator + 7 test PDFs
static/index.html UI: live run view + dashboard
```

## What I'd build next
Email inbox ingestion · LLM extraction by default with field-level confidence · reviewer actions (approve-override with reason) · ERP connector (Tally / NetSuite) · multi-currency · line-level 3-way match against GRNs.
