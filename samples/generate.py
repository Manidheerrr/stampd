"""Generates the demo invoice PDFs. Run: python samples/generate.py"""
import os, subprocess, tempfile, random
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor, black
from PIL import Image, ImageFilter

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pdfs")
os.makedirs(OUT, exist_ok=True)
W, H = A4


def money(x):
    return f"{x:,.2f}"


def invoice(path, *, vendor, address, accent, labels, inv_no, date, po, items,
            tax_rate=0.18, bank=None, notes=None, bill_to="Meridian Retail Pvt Ltd"):
    c = canvas.Canvas(path, pagesize=A4)
    c.setFillColor(HexColor(accent)); c.rect(0, H - 18, W, 18, stroke=0, fill=1)
    c.setFillColor(black); c.setFont("Helvetica-Bold", 20); c.drawString(40, H - 70, vendor)
    c.setFont("Helvetica", 9)
    for i, line in enumerate(address):
        c.drawString(40, H - 88 - i * 12, line)
    c.setFont("Helvetica-Bold", 16); c.drawRightString(W - 40, H - 70, labels.get("title", "TAX INVOICE"))
    c.setFont("Helvetica", 10)
    y = H - 160
    c.drawString(40, y, f"{labels['no']}: {inv_no}")
    c.drawString(40, y - 15, f"{labels['date']}: {date}")
    if po:
        c.drawString(40, y - 30, f"{labels['po']}: {po}")
    c.drawString(330, y, "Bill To:"); c.drawString(330, y - 15, bill_to); c.drawString(330, y - 30, "Anna Salai, Chennai 600002")
    y -= 75
    c.setFillColor(HexColor(accent)); c.rect(40, y - 4, W - 80, 18, stroke=0, fill=1)
    c.setFillColor(HexColor("#ffffff")); c.setFont("Helvetica-Bold", 9)
    c.drawString(46, y + 1, "Description"); c.drawRightString(360, y + 1, "Qty")
    c.drawRightString(450, y + 1, "Rate"); c.drawRightString(W - 46, y + 1, "Amount")
    c.setFillColor(black); c.setFont("Helvetica", 10)
    sub = 0
    for desc, qty, rate in items:
        y -= 22; amt = qty * rate; sub += amt
        c.drawString(46, y, desc); c.drawRightString(360, y, str(qty))
        c.drawRightString(450, y, money(rate)); c.drawRightString(W - 46, y, money(amt))
    tax = round(sub * tax_rate, 2); total = round(sub + tax, 2)
    y -= 20; c.line(300, y + 10, W - 40, y + 10)
    c.drawString(310, y - 5, labels.get("sub", "Subtotal")); c.drawRightString(W - 46, y - 5, money(sub))
    c.drawString(310, y - 22, f"GST @ {int(tax_rate*100)}%"); c.drawRightString(W - 46, y - 22, money(tax))
    c.setFont("Helvetica-Bold", 11)
    c.drawString(310, y - 42, labels.get("total", "Grand Total") + " (INR)"); c.drawRightString(W - 46, y - 42, money(total))
    c.setFont("Helvetica", 9); y -= 90
    if bank:
        c.drawString(40, y, "Bank Details"); c.drawString(40, y - 13, f"Account No: {bank[0]}"); c.drawString(40, y - 26, f"IFSC: {bank[1]}")
        y -= 45
    for n in (notes or []):
        c.drawString(40, y, n); y -= 13
    c.setFont("Helvetica-Oblique", 8); c.drawString(40, 40, "This is a computer generated invoice.")
    c.save()
    return sub, tax, total


def scanify(pdf_path):
    """Turn a clean PDF into an image-only 'scanned' PDF (slight skew + blur + grain)."""
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["pdftoppm", "-r", "170", "-png", pdf_path, os.path.join(d, "p")], check=True)
        png = [f for f in os.listdir(d) if f.endswith(".png")][0]
        im = Image.open(os.path.join(d, png)).convert("L")
        im = im.rotate(0.6, expand=True, fillcolor=245).filter(ImageFilter.GaussianBlur(0.45))
        random.seed(7); px = im.load()
        for _ in range(9000):
            x, y = random.randrange(im.width), random.randrange(im.height)
            px[x, y] = min(255, px[x, y] + random.randint(-35, 10)) if px[x, y] > 200 else px[x, y]
        im = Image.merge("RGB", (im, im, im.point(lambda v: max(0, v - 6))))
        im.save(pdf_path, "PDF", resolution=170)


NW = dict(vendor="Northwind Office Supplies Pvt Ltd", address=["14 Industrial Estate, Guindy", "Chennai 600032 | GSTIN 33AAACN4421K1Z5"],
          accent="#1d4ed8", labels={"no": "Invoice No", "date": "Invoice Date", "po": "PO Ref"})
KL = dict(vendor="Kestrel Logistics", address=["Plot 9, SIPCOT, Sriperumbudur", "GSTIN 33AAFCK7781L1Z2"],
          accent="#0f766e", labels={"no": "Bill #", "date": "Dated", "po": "Purchase Order", "total": "Amount Due", "title": "INVOICE"})

if __name__ == "__main__":
    p = lambda n: os.path.join(OUT, n)
    invoice(p("01_happy_path_northwind.pdf"), **NW, inv_no="INV-NW-3381", date="02-Nov-2026", po="PO-2026-1041",
            items=[("A4 Copier Paper 75gsm (box)", 200, 450.00), ("Laser Toner Cartridge 88A", 4, 2500.00)],
            bank=("918020045573821", "UTIB0000412"))
    invoice(p("02_within_tolerance_kestrel.pdf"), **KL, inv_no="KL-5520", date="04-Nov-2026", po="PO-2026-1063",
            items=[("FTL freight Chennai-Pune (32ft)", 6, 35000.00), ("Fuel surcharge (Oct index)", 1, 4830.00)],
            bank=("50100234414417", "HDFC0001267"))
    invoice(p("03_split_po_overbilled_brightline.pdf"), vendor="Brightline IT Services", address=["Level 4, Tidel Park, Taramani", "GSTIN 33AAGCB1290M1Z8"],
            accent="#7c3aed", labels={"no": "Invoice Number", "date": "Date", "po": "Purchase Order", "total": "Total Payable", "sub": "Taxable Value"},
            inv_no="BL-917", date="05-Nov-2026", po="PO-2026-1077",
            items=[("ERP integration - Milestone 2 of 2", 1, 150000.00), ("Change request CR-14 (data migration)", 1, 30000.00)],
            bank=("039105002211", "ICIC0000391"), notes=["Milestone 1 billed on invoice BL-901."])
    invoice(p("04_duplicate_resubmitted_northwind.pdf"), **NW, inv_no="INV/NW/3290", date="14-Oct-2026", po="PO-2026-1029",
            items=[("Stationery restock kit", 50, 1000.00)], bank=("918020045573821", "UTIB0000412"),
            notes=["REMINDER: payment overdue, please process at the earliest."])
    scanned = p("05_scanned_no_po_apex.pdf")
    invoice(scanned, vendor="Apex Packaging Co", address=["22 Ambattur Industrial Estate, Chennai", "GSTIN 33AABCA5532P1Z1"],
            accent="#b45309", labels={"no": "Invoice No", "date": "Invoice Date", "po": "PO"},
            inv_no="AX-7781", date="06-Nov-2026", po=None,
            items=[("Corrugated box 5-ply 18x12x12", 5000, 15.00)], bank=("20381177307730", "SBIN0004431"))
    scanify(scanned)
    invoice(p("06_bank_details_changed_kestrel.pdf"), **KL, inv_no="KL-5534", date="07-Nov-2026", po="PO-2026-1064",
            items=[("Last-mile delivery, Chennai metro", 1, 60000.00)], bank=("50200098761234", "KKBK0000958"),
            notes=["IMPORTANT: We have changed our bank. Kindly remit to the new account above."])
    invoice(p("07_unapproved_vendor_quickfix.pdf"), vendor="Quickfix Facilities", address=["Old Mahabalipuram Rd, Chennai", "GSTIN 33AAJFQ0021C1Z9"],
            accent="#be123c", labels={"no": "Invoice No", "date": "Invoice Date", "po": "PO Ref"},
            inv_no="QF-0042", date="08-Nov-2026", po="PO-2026-1041",
            items=[("AC servicing, 3rd floor", 12, 1800.00)], bank=("7711002233445", "YESB0000123"))
    print("\n".join(sorted(os.listdir(OUT))))
