"""
Invoice / Quotation PDF generator (ReportLab).

Layout is defined in "design pixels" measured from the sample quotation
(708 x 990) and mapped onto A4.
"""
from __future__ import annotations

import io
import os
from datetime import date
from typing import Any

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

COLS = {
    "id": (85, 138),
    "desc": (138, 373),
    "unit": (373, 429),
    "qty": (429, 477),
    "rate": (477, 557),
    "total": (557, 649),
}
TABLE_LEFT, TABLE_RIGHT = 85, 649

DEFAULT_PAYMENT_TERMS = "Advance – 70% Up front balance after completion"
DEFAULT_NOTES_QUOTATION = (
    "Please feel free to call us on any matter arising from the above.\n"
    "This Quotation will valid only for 15 days."
)
DEFAULT_NOTES_INVOICE = "Please feel free to call us on any matter arising from the above."


def X(px: float) -> float:
    return px * SX


def Y(py: float) -> float:
    return PAGE_H - py * SY


def money(v: float) -> str:
    return f"{v:,.2f}"


def rate_fmt(v: float) -> str:
    return f"{v:,.0f}/-" if float(v).is_integer() else f"{v:,.2f}/-"


def draw_asset(c: canvas.Canvas, name: str, top_px: float, bottom_px: float) -> None:
    path = os.path.join(ASSETS, name)
    if os.path.exists(path):
        c.drawImage(
            ImageReader(path),
            0,
            Y(bottom_px),
            PAGE_W,
            (bottom_px - top_px) * SY,
            mask="auto",
        )


def draw_text(
    c: canvas.Canvas,
    px: float,
    py: float,
    text: str,
    font: str = FONT,
    size: float = 9.5,
    align: str = "left",
) -> None:
    c.setFont(font, size)
    x, y = X(px), Y(py)
    if align == "right":
        c.drawRightString(x, y, text)
    elif align == "center":
        c.drawCentredString(x, y, text)
    else:
        c.drawString(x, y, text)


def _doc_labels(doc_type: str) -> dict[str, str]:
    if doc_type == "quotation":
        return {
            "title": "Quotation",
            "number_prefix": "Quotation ",
            "to_label": "Quote To",
            "for_label": "Quotation for",
        }
    return {
        "title": "INVOICE",
        "number_prefix": "Inv No.",
        "to_label": "Invoice To",
        "for_label": "Invoice for",
    }


def compute_subtotal(items: list[dict[str, Any]]) -> float:
    total = 0.0
    for item in items:
        line_total = item["qty"] * item["rate"]
        if item.get("total") is not None:
            line_total = item["total"]
        total += float(line_total)
    return total


def compute_grand_total(
    items: list[dict[str, Any]],
    transportation: float = 0,
    discount: float = 0,
) -> float:
    """Grand total = subtotal + transportation − discount."""
    return compute_subtotal(items) + float(transportation or 0) - float(discount or 0)


def default_invoice_date() -> str:
    return date.today().strftime("%d/%m/%Y")


def _draw_info_cell(
    c: canvas.Canvas,
    left: float,
    top: float,
    width: float,
    height: float,
    label: str,
    value_lines: list[str],
) -> None:
    """Grey label strip on left of a bordered value cell (sample quotation style)."""
    label_w = 78
    c.setStrokeColor(black)
    c.setLineWidth(0.6)
    c.setFillColor(GREY)
    c.rect(X(left), Y(top + height), X(label_w), height * SY, fill=1, stroke=1)
    c.setFillColor(black)
    c.rect(X(left + label_w), Y(top + height), X(width - label_w), height * SY, fill=0, stroke=1)
    draw_text(c, left + 4, top + 12, label, size=8)
    ty = top + 12
    for line in value_lines[:4]:
        draw_text(c, left + label_w + 5, ty, line, size=9)
        ty += 14


def generate_invoice(data: dict[str, Any]) -> bytes:
    """Generate Invoice or Quotation PDF bytes (same function for both doc types)."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)

    doc_type = (data.get("doc_type") or "invoice").lower()
    if doc_type not in ("invoice", "quotation"):
        doc_type = "invoice"
    labels = _doc_labels(doc_type)
    number = str(data["invoice_no"])
    c.setTitle(f"{labels['title']} {number}")

    draw_asset(c, "header.png", 0, 124)
    draw_asset(c, "footer.png", 893, 990)

    c.setFillColor(black)
    draw_text(c, 673, 143, f"{labels['number_prefix']}{number}", size=9, align="right")

    # Title with rules
    c.setStrokeColor(black)
    c.setLineWidth(0.8)
    c.line(X(85), Y(158), X(300), Y(158))
    c.line(X(408), Y(158), X(623), Y(158))
    draw_text(c, 354, 162, labels["title"], font=BOLD, size=12, align="center")

    customer = data.get("customer") or {}
    cust_lines = [customer.get("name", ""), *customer.get("address_lines", [])]
    cust_lines = [ln for ln in cust_lines if ln]
    deliver = data.get("deliver_to") or []
    if not deliver and data.get("site"):
        deliver = [data["site"]]
    attention = data.get("attention") or []
    att_text = ", ".join(attention) if attention else ""
    subject = data.get("subject") or ""
    duration = data.get("duration") or ""
    date_str = data.get("date") or default_invoice_date()

    # Info grid — two columns
    left_x, right_x = 85, 380
    cell_w_l, cell_w_r = 280, 269
    row1_h, row2_h = 48, 36
    top = 175

    _draw_info_cell(c, left_x, top, cell_w_l, row1_h, labels["to_label"], cust_lines or [""])
    _draw_info_cell(c, right_x, top, cell_w_r, row1_h / 2, "Date", [date_str])
    _draw_info_cell(
        c, right_x, top + row1_h / 2, cell_w_r, row1_h / 2, "Duration", [duration]
    )

    top2 = top + row1_h
    _draw_info_cell(c, left_x, top2, cell_w_l, row2_h, "Deliver To", deliver or [""])
    _draw_info_cell(c, right_x, top2, cell_w_r, row2_h / 2, "Attention", [att_text])
    _draw_info_cell(
        c,
        right_x,
        top2 + row2_h / 2,
        cell_w_r,
        row2_h / 2,
        labels["for_label"],
        [subject],
    )

    # Table
    top = top2 + row2_h + 12
    header_h = 17
    c.setLineWidth(0.7)
    c.setFillColor(GREY)
    c.rect(
        X(TABLE_LEFT),
        Y(top + header_h),
        X(TABLE_RIGHT - TABLE_LEFT),
        header_h * SY,
        fill=1,
        stroke=0,
    )
    c.setFillColor(black)
    c.setStrokeColor(black)
    headers = {
        "id": "ID",
        "desc": "Description",
        "unit": "Unit",
        "qty": "Qty",
        "rate": "Rate(Rs)",
        "total": "Total (RS.)",
    }
    for key, (l, r) in COLS.items():
        c.rect(X(l), Y(top + header_h), X(r - l), header_h * SY, fill=0, stroke=1)
        if key in ("id", "desc", "unit"):
            draw_text(c, l + 5, top + 12, headers[key], size=9)
        else:
            draw_text(c, (l + r) / 2, top + 12, headers[key], size=9, align="center")
    top += header_h

    items = data.get("items") or []
    desc_width = X(COLS["desc"][1] - COLS["desc"][0] - 12)
    for i, item in enumerate(items, start=1):
        line_total = item["qty"] * item["rate"]
        if item.get("total") is not None:
            line_total = item["total"]

        title = (item.get("description") or "").strip()
        details = [d.strip() for d in (item.get("details") or []) if d and str(d).strip()]
        # Back-compat: if description contains newlines, split into title + details
        if "\n" in title and not details:
            parts = [p.strip() for p in title.split("\n") if p.strip()]
            title = parts[0] if parts else title
            details = parts[1:]

        title_lines = simpleSplit(title, BOLD, 9.5, desc_width)
        detail_line_groups: list[list[str]] = []
        for detail in details:
            bullet = f"• {detail}"
            detail_line_groups.append(simpleSplit(bullet, FONT, 9, desc_width - X(8)))

        n_lines = len(title_lines) + sum(len(g) for g in detail_line_groups)
        row_h = max(36, 14 * n_lines + 18)
        c.setFillColor(GREY)
        c.rect(
            X(TABLE_LEFT),
            Y(top + row_h),
            X(TABLE_RIGHT - TABLE_LEFT),
            row_h * SY,
            fill=1,
            stroke=0,
        )
        c.setFillColor(black)
        for key, (l, r) in COLS.items():
            c.rect(X(l), Y(top + row_h), X(r - l), row_h * SY, fill=0, stroke=1)

        ty = top + 14
        draw_text(c, COLS["id"][0] + 8, ty, f"{i:03d}")

        # Title: bold + underline
        for j, ln in enumerate(title_lines):
            c.setFont(BOLD, 9.5)
            x0 = X(COLS["desc"][0] + 5)
            y0 = Y(ty + j * 14)
            c.drawString(x0, y0, ln)
            c.setStrokeColor(black)
            c.setLineWidth(0.5)
            c.line(x0, y0 - 1.5, x0 + c.stringWidth(ln, BOLD, 9.5), y0 - 1.5)
        ty_cursor = ty + 14 * len(title_lines)

        # Bullet details
        for group in detail_line_groups:
            for ln in group:
                c.setFont(FONT, 9)
                c.drawString(X(COLS["desc"][0] + 10), Y(ty_cursor), ln)
                ty_cursor += 14

        draw_text(
            c,
            (COLS["unit"][0] + COLS["unit"][1]) / 2,
            ty,
            item.get("unit", "No.s"),
            align="center",
        )
        qty = item["qty"]
        qty_str = f"{int(qty):02d}" if float(qty).is_integer() else str(qty)
        draw_text(c, (COLS["qty"][0] + COLS["qty"][1]) / 2, ty, qty_str, align="center")
        draw_text(c, COLS["rate"][1] - 5, ty, rate_fmt(item["rate"]), align="right")
        draw_text(c, COLS["total"][1] - 5, ty, money(line_total), align="right")
        top += row_h

    transportation = float(data.get("transportation") or 0)
    discount = float(data.get("discount") or 0)
    subtotal = compute_subtotal(items)
    grand = subtotal + transportation - discount

    def _summary_row(label: str, value: float, *, bold: bool = False) -> None:
        nonlocal top
        tot_h = 18
        c.setFillColor(GREY)
        c.rect(
            X(TABLE_LEFT),
            Y(top + tot_h),
            X(TABLE_RIGHT - TABLE_LEFT),
            tot_h * SY,
            fill=1,
            stroke=0,
        )
        c.setFillColor(black)
        c.rect(
            X(COLS["total"][0]),
            Y(top + tot_h),
            X(COLS["total"][1] - COLS["total"][0]),
            tot_h * SY,
            fill=0,
            stroke=1,
        )
        font = BOLD if bold else FONT
        draw_text(c, COLS["total"][0] - 5, top + 12, label, font=font, size=9, align="right")
        draw_text(c, COLS["total"][1] - 5, top + 12, money(value), font=font, size=9, align="right")
        top += tot_h

    _summary_row("Subtotal (Rs.)", subtotal)
    _summary_row("Transportation", transportation)
    _summary_row("Discount", discount)
    _summary_row("Grand Total (Rs.)", grand, bold=True)

    # Payment terms + notes
    payment = data.get("payment_terms") or DEFAULT_PAYMENT_TERMS
    notes = data.get("notes")
    if not notes:
        notes = DEFAULT_NOTES_QUOTATION if doc_type == "quotation" else DEFAULT_NOTES_INVOICE

    top += 14
    draw_text(c, 85, top + 10, "Payment Terms", font=BOLD, size=9)
    draw_text(c, 85, top + 26, payment, size=9)

    notes_top = top + 44
    note_lines = []
    for para in str(notes).split("\n"):
        note_lines.extend(simpleSplit(para, FONT, 9, X(TABLE_RIGHT - TABLE_LEFT - 10)))
    note_h = max(40, 14 * len(note_lines) + 16)
    c.setFillColor(GREY)
    c.rect(
        X(TABLE_LEFT),
        Y(notes_top + note_h),
        X(TABLE_RIGHT - TABLE_LEFT),
        note_h * SY,
        fill=1,
        stroke=1,
    )
    c.setFillColor(black)
    draw_text(c, 90, notes_top + 12, "Notes", font=BOLD, size=9)
    ny = notes_top + 26
    for ln in note_lines:
        draw_text(c, 90, ny, ln, size=8.5)
        ny += 12

    # Sign-off
    sig_y = max(notes_top + note_h + 30, 720)
    draw_text(c, 117, sig_y, "Thanking you,", size=10)
    draw_text(c, 117, sig_y + 16, "Yours Sincerely,", font=BOLD, size=10)
    draw_text(
        c,
        117,
        sig_y + 34,
        data.get("company", "HAMILTON DE SILVA & SONS"),
        font=BOLD,
        size=10,
    )
    sig = os.path.join(ASSETS, "signature.png")
    if data.get("include_signature", True) and os.path.exists(sig):
        c.drawImage(
            ImageReader(sig),
            X(112),
            Y(sig_y + 90),
            X(83),
            37 * SY,
            mask="auto",
        )
    draw_text(c, 117, sig_y + 100, data.get("signatory", "Nilantha De Silva"), size=10)

    c.showPage()
    c.save()
    return buf.getvalue()


# Alias used by newer call sites
generate_document = generate_invoice
