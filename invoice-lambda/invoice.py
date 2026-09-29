"""
Invoice PDF generator (ReportLab) + AWS Lambda handler.

Layout is defined in "design pixels" measured from the sample invoice
(708 x 990) and mapped onto A4, so it is easy to nudge things around.
"""
import io
import json
import os
from datetime import date

from reportlab.lib.colors import HexColor, black
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfgen import canvas

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")

PAGE_W, PAGE_H = A4
DESIGN_W, DESIGN_H = 708, 990
SX, SY = PAGE_W / DESIGN_W, PAGE_H / DESIGN_H

GREY = HexColor("#D9D9D9")
FONT, BOLD = "Helvetica", "Helvetica-Bold"

# Table columns in design px: (left edge, right edge)
COLS = {
    "id": (85, 138),
    "desc": (138, 373),
    "unit": (373, 429),
    "qty": (429, 477),
    "rate": (477, 557),
    "total": (557, 649),
}
TABLE_LEFT, TABLE_RIGHT = 85, 649


def X(px):
    return px * SX


def Y(py):
    return PAGE_H - py * SY


def money(v):
    return f"{v:,.2f}"


def rate_fmt(v):
    # sample shows "75,000/-"
    return f"{v:,.0f}/-" if float(v).is_integer() else f"{v:,.2f}/-"


def draw_asset(c, name, top_px, bottom_px):
    path = os.path.join(ASSETS, name)
    if os.path.exists(path):
        c.drawImage(
            ImageReader(path), 0, Y(bottom_px), PAGE_W, (bottom_px - top_px) * SY,
            mask="auto",
        )


def draw_text(c, px, py, text, font=FONT, size=9.5, align="left"):
    c.setFont(font, size)
    x, y = X(px), Y(py)
    if align == "right":
        c.drawRightString(x, y, text)
    elif align == "center":
        c.drawCentredString(x, y, text)
    else:
        c.drawString(x, y, text)


def draw_label_underlined(c, px, py, label, size=9.5):
    draw_text(c, px, py, label, size=size)
    w = c.stringWidth(label, FONT, size)
    c.setLineWidth(0.5)
    c.line(X(px), Y(py) - 1.5, X(px) + w, Y(py) - 1.5)
    return w


def generate_invoice(data: dict) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle(f"Invoice {data['invoice_no']}")

    # Letterhead
    draw_asset(c, "header.png", 0, 124)
    draw_asset(c, "footer.png", 893, 990)

    c.setFillColor(black)

    # Top block
    draw_text(c, 673, 143, f"Inv No.{data['invoice_no']}", size=9, align="right")
    draw_text(c, 354, 160, "INVOICE", font=BOLD, size=12, align="center")
    draw_text(c, 67, 175, data["date"])
    draw_text(c, 67, 204, "Invoice To")

    y = 234
    for line in [data["customer"]["name"], *data["customer"].get("address_lines", [])]:
        draw_text(c, 67, y, line)
        y += 16

    # Attention / Site
    att = data.get("attention", [])
    if att:
        w = draw_label_underlined(c, 67, 298, "Attention:-")
        ax = 67 + w / SX + 4
        for i, name in enumerate(att):
            draw_text(c, ax, 298 + i * 16, name)
    draw_label_underlined(c, 406, 298, "Site:-")
    draw_text(c, 406 + 34, 298, data.get("site", ""))

    # Table header
    top = 335
    header_h = 17
    c.setLineWidth(0.7)
    c.setFillColor(GREY)
    c.rect(X(TABLE_LEFT), Y(top + header_h), X(TABLE_RIGHT - TABLE_LEFT), header_h * SY, fill=1, stroke=0)
    c.setFillColor(black)
    c.setStrokeColor(black)
    headers = {"id": "ID", "desc": "Description", "unit": "Unit", "qty": "Qty",
               "rate": "Rate(Rs)", "total": "Total (RS.)"}
    for key, (l, r) in COLS.items():
        c.rect(X(l), Y(top + header_h), X(r - l), header_h * SY, fill=0, stroke=1)
        if key in ("id", "desc", "unit"):
            draw_text(c, l + 5, top + 12, headers[key], size=9)
        else:
            draw_text(c, (l + r) / 2, top + 12, headers[key], size=9, align="center")
    top += header_h

    # Item rows
    grand_total = 0.0
    for i, item in enumerate(data["items"], start=1):
        line_total = item["qty"] * item["rate"]
        if "total" in item:  # allow override (sample has a rounded line total)
            line_total = item["total"]
        grand_total += line_total

        desc_lines = simpleSplit(item["description"], FONT, 9.5, X(COLS["desc"][1] - COLS["desc"][0] - 10))
        row_h = max(36, 14 * len(desc_lines) + 18)
        c.setFillColor(GREY)
        c.rect(X(TABLE_LEFT), Y(top + row_h), X(TABLE_RIGHT - TABLE_LEFT), row_h * SY, fill=1, stroke=0)
        c.setFillColor(black)
        for key, (l, r) in COLS.items():
            c.rect(X(l), Y(top + row_h), X(r - l), row_h * SY, fill=0, stroke=1)

        ty = top + 14
        draw_text(c, COLS["id"][0] + 8, ty, f"{i:03d}")
        for j, ln in enumerate(desc_lines):
            c.setFont(FONT, 9.5)
            c.drawString(X(COLS["desc"][0] + 5), Y(ty + j * 14), ln)
        draw_text(c, (COLS["unit"][0] + COLS["unit"][1]) / 2, ty, item.get("unit", "No.s"), align="center")
        draw_text(c, (COLS["qty"][0] + COLS["qty"][1]) / 2, ty, f"{item['qty']:02d}", align="center")
        draw_text(c, COLS["rate"][1] - 5, ty, rate_fmt(item["rate"]), align="right")
        draw_text(c, COLS["total"][1] - 5, ty, money(line_total), align="right")
        top += row_h

    # Total row
    tot_h = 21
    c.setFillColor(GREY)
    c.rect(X(TABLE_LEFT), Y(top + tot_h), X(TABLE_RIGHT - TABLE_LEFT), tot_h * SY, fill=1, stroke=0)
    c.setFillColor(black)
    c.rect(X(COLS["total"][0]), Y(top + tot_h), X(COLS["total"][1] - COLS["total"][0]), tot_h * SY, fill=0, stroke=1)
    draw_text(c, COLS["total"][0] - 5, top + 14, "Total (Rs.)", size=9.5, align="right")
    draw_text(c, COLS["total"][1] - 5, top + 14, money(grand_total), size=9.5, align="right")

    # Sign-off
    draw_text(c, 117, 762, "Yours Sincerely,", font=BOLD, size=10)
    draw_text(c, 117, 785, data.get("company", "HAMILTON DE SILVA & SONS"), font=BOLD, size=10)
    sig = os.path.join(ASSETS, "signature.png")
    if data.get("include_signature", True) and os.path.exists(sig):
        c.drawImage(ImageReader(sig), X(112), Y(842), X(83), 37 * SY, mask="auto")
    draw_text(c, 117, 865, data.get("signatory", "Nilantha De Silva"), size=10)

    c.showPage()
    c.save()
    return buf.getvalue()


# ---------------------------------------------------------------- Lambda ----
def lambda_handler(event, context):
    """Accepts the invoice JSON directly (or as API Gateway `body`),
    returns the PDF as base64 so it works behind API Gateway / Function URL.
    In the WhatsApp flow you'd instead upload to S3 / WhatsApp media here."""
    import base64

    payload = event.get("body", event)
    if isinstance(payload, str):
        payload = json.loads(payload)
    payload.setdefault("date", date.today().strftime("%d/%m/%Y"))

    pdf = generate_invoice(payload)
    return {
        "statusCode": 200,
        "isBase64Encoded": True,
        "headers": {
            "Content-Type": "application/pdf",
            "Content-Disposition": f'inline; filename="invoice-{payload["invoice_no"]}.pdf"',
        },
        "body": base64.b64encode(pdf).decode(),
    }
