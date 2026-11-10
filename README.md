# Stampd: invoice processing, from PDF to an explained decision

**Zamp ASA case study · PS-1 (Finance / Accounts Payable)** · Live: https://stampd-jokj.onrender.com

Stampd takes a vendor invoice PDF (native or scanned) and decides **APPROVE**, **HOLD** or **REJECT**. Every decision comes with:
- reason codes, in plain English
- where the invoice is routed (payment run, AP reviewer, procurement, treasury)
- a drafted email to the vendor where one is needed
- a reviewer step: approve or reject, with a mandatory reason when overruling the system
- a downloadable audit record

> On Render's free tier the first visit after a quiet period takes about 50 seconds while the server wakes up.

## Design principle: AI reads, rules decide
OCR and extraction only **read** the invoice. Every payment decision is a **deterministic rule**, so the same invoice always gets the same answer and every answer can be explained to an auditor. Extraction can be swapped for Claude (set `ANTHROPIC_API_KEY`) without changing any decision logic.

## Pipeline (each stage streams live in the UI)
| # | Stage | What it does |
|---|---|---|
| 1 | Ingest | Detects a text layer or a scan; runs OCR with a confidence score. Rejects encrypted, corrupt or oversized PDFs safely. |
| 2 | Extract | Vendor, invoice no., date, PO, currency, subtotal, tax (CGST+SGST/IGST summed), total, bank a/c, IFSC, line items (optional HSN). Handles many label variants and date formats. Fills one missing amount from the other two and marks it *derived*. |
| 3 | Validate | Mandatory fields; line items add up to the subtotal; subtotal + tax = total; valid GST slab; date not in the future or stale; currency matches the POs. |
| 4 | Vendor | Fuzzy match against the vendor master; vendor approved (KYC done); **bank account matches the verified one**. |
| 5 | PO match | Explicit PO, or **inferred** from vendor + amount (always held for a human). Checks the **remaining** PO balance, which catches split invoices, and applies a 2% **and** ₹10k tolerance. |
| 6 | Duplicate | Normalised invoice numbers (`INV/NW/3290` = `INV-NW-3290`) plus vendor + amount + PO, against the payment ledger. |
| 7 | Decision | Severity roll-up into a decision, route, headline, vendor email and audit record. |

## Test deck (built into the app)
| # | Scenario | Result |
|---|---|---|
| 01 | Clean invoice, exact PO match | APPROVE |
| 02 | 1.4% over the PO, inside tolerance | APPROVE (variance noted) |
| 03 | Split PO: 2nd milestone pushes cumulative billing past the PO | HOLD → Procurement |
| 04 | Disguised duplicate (`INV/NW/3290` vs `INV-NW-3290`) | REJECT |
| 05 | Scanned image, no PO printed | HOLD (OCR + inferred PO) |
| 06 | Bank details changed (payment-fraud pattern) | HOLD → Treasury |
| 07 | Vendor still in KYC | REJECT |
| 08 | Different layout: CGST+SGST, "Rs.", dd/mm/yyyy, HSN column | APPROVE |
| 09 | Line items don't add up to the subtotal | HOLD + vendor email |
| 10 | Billed in USD against an INR PO | HOLD → Treasury (FX) |

Approved invoices are written to the ledger, so re-running one is caught as a duplicate. **Reset demo** restores the seeded ledger.

## Reviewer step
Every finished run has a reviewer panel. Confirming the system's decision needs no comment. **Overruling it requires a reason** (8+ characters), which is stored with the reviewer's name and time. Duplicates can never be approved. Approvals go into the payment ledger, which then feeds duplicate detection and PO-balance checks.

## API
`POST /api/runs` (file or `sample`) · `GET /api/runs` · `GET /api/runs/{id}` · `GET /api/runs/{id}/pdf` · `GET /api/runs/{id}/audit` · `POST /api/runs/{id}/review` · `GET /api/config` · `POST /api/reset`

## Run locally
- **Windows:** double-click `start.bat`. It installs Python if needed and opens http://localhost:8000.
- **Mac/Linux:** `brew install tesseract poppler` (or apt), then `./run.sh`.
- **Docker:** `docker build -t stampd . && docker run -p 8000:8000 stampd`.

**Optional Claude extraction:** set `ANTHROPIC_API_KEY` (and optionally `LLM_MODEL`). If the API call fails, it falls back to the rules parser.

## Assumptions
- One invoice per PDF. The vendor master, POs and ledger are JSON/SQLite stand-ins for an ERP.
- Amounts are tax-inclusive INR. Foreign currency is always held for treasury.
- Tolerance is a dual cap (≤2% **and** ≤₹10,000), so large invoices can't hide big rupee variances.
- An inferred PO or a bank change is **always** held, however clean everything else looks.

## Layout
```
app/pipeline.py   7-stage decision pipeline and rules
app/extract.py    PDF text / OCR + rules parser + optional Claude extractor
app/main.py       API, SQLite run store and ledger, reviewer actions, audit export
data/master.json  vendors, POs, policy, seeded payment ledger
samples/          invoice generator (10 scenarios) + cached OCR for Windows
static/index.html UI: live run, compare-with-invoice view, reviewer panel, dashboard
```
