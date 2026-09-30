"""
Invoice / Quotation PDF generator (ReportLab).

Layout is defined in "design pixels" measured from the sample quotation
(708 x 990) and mapped onto A4. Long item lists flow onto extra pages so
content never overlaps the footer or signature.
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

# Design-Y limit for table rows (footer image starts ~893)
CONTENT_BOTTOM = 760
# Closing block (payment / notes / signature) needs this much space
CLOSING_RESERVE = 220
# Continuation pages start table below the "Quotation (cont.)" / "INVOICE (cont.)" title
CONT_TABLE_TOP = 190

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
        }
    return {
        "title": "INVOICE",
        "number_prefix": "Inv No.",
        "to_label": "Invoice To",
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
    advance: float = 0,
) -> float:
    """Balance due = subtotal + transportation − discount − advance."""
    return (
        compute_subtotal(items)
        + float(transportation or 0)
        - float(discount or 0)
        - float(advance or 0)
    )


def default_invoice_date() -> str:
    return date.today().strftime("%d/%m/%Y")


def _info_value_width(cell_width: float, label_w: float = 78) -> float:
    return X(cell_width - label_w - 12)


def _wrap_info_lines(value_lines: list[str], cell_width: float, *, max_lines: int = 6) -> list[str]:
    width = _info_value_width(cell_width)
    wrapped: list[str] = []
    for raw in value_lines:
        text = (raw or "").strip()
        if not text:
            continue
        wrapped.extend(simpleSplit(text, FONT, 9, width))
    if not wrapped:
        return [""]
    return wrapped[:max_lines]


def _info_cell_height(value_lines: list[str], *, min_h: float = 24) -> float:
    n = max(1, len(value_lines))
    return max(min_h, 14 * n + 10)


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
    for line in value_lines:
        draw_text(c, left + label_w + 5, ty, line, size=9)
        ty += 14


def _draw_page_chrome(c: canvas.Canvas, labels: dict[str, str], number: str, *, continued: bool = False) -> None:
    draw_asset(c, "header.png", 0, 124)
    draw_asset(c, "footer.png", 893, 990)
    c.setFillColor(black)
    draw_text(c, 673, 143, f"{labels['number_prefix']}{number}", size=9, align="right")
    c.setStrokeColor(black)
    c.setLineWidth(0.8)
    c.line(X(85), Y(158), X(300), Y(158))
    c.line(X(408), Y(158), X(623), Y(158))
    title = labels["title"] + (" (cont.)" if continued else "")
    draw_text(c, 354, 162, title, font=BOLD, size=12, align="center")


def _draw_table_header(c: canvas.Canvas, top: float) -> float:
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
    return top + header_h


def _prepare_item_layout(item: dict[str, Any], desc_width: float) -> dict[str, Any]:
    title = (item.get("description") or "").strip()
    details = [d.strip() for d in (item.get("details") or []) if d and str(d).strip()]
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
    line_total = item["qty"] * item["rate"]
    if item.get("total") is not None:
        line_total = item["total"]
    return {
        "title_lines": title_lines,
        "detail_line_groups": detail_line_groups,
        "row_h": row_h,
        "line_total": line_total,
        "item": item,
    }


def _draw_item_row(c: canvas.Canvas, top: float, index: int, layout: dict[str, Any]) -> float:
    row_h = layout["row_h"]
    item = layout["item"]
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
    draw_text(c, COLS["id"][0] + 8, ty, f"{index:03d}")

    for j, ln in enumerate(layout["title_lines"]):
        c.setFont(BOLD, 9.5)
        x0 = X(COLS["desc"][0] + 5)
        y0 = Y(ty + j * 14)
        c.drawString(x0, y0, ln)
        c.setStrokeColor(black)
        c.setLineWidth(0.5)
        c.line(x0, y0 - 1.5, x0 + c.stringWidth(ln, BOLD, 9.5), y0 - 1.5)
    ty_cursor = ty + 14 * len(layout["title_lines"])

    for group in layout["detail_line_groups"]:
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
    draw_text(c, COLS["total"][1] - 5, ty, money(layout["line_total"]), align="right")
    return top + row_h


def _new_continuation_page(
    c: canvas.Canvas,
    labels: dict[str, str],
    number: str,
) -> float:
    c.showPage()
    _draw_page_chrome(c, labels, number, continued=True)
    return _draw_table_header(c, CONT_TABLE_TOP)


def _draw_closing(
    c: canvas.Canvas,
    top: float,
    *,
    data: dict[str, Any],
    doc_type: str,
    subtotal: float,
    transportation: float,
    discount: float,
    advance: float,
    grand: float,
) -> None:
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
    if transportation:
        _summary_row("Transportation", transportation)
    if discount:
        _summary_row("Discount", discount)
    if advance:
        note = (data.get("advance_note") or "").strip()
        adv_label = f"Advance ({note})" if note else "Advance"
        _summary_row(adv_label, advance)
        _summary_row("Total Balance (Rs.)", grand, bold=True)
    else:
        _summary_row("Grand Total (Rs.)", grand, bold=True)

    payment = (data.get("payment_terms") or "").strip()
    notes = (data.get("notes") or "").strip()
    content_bottom = top

    if payment:
        pay_lines = simpleSplit(payment, FONT, 9, X(TABLE_RIGHT - TABLE_LEFT - 10))
        top += 14
        draw_text(c, 85, top + 10, "Payment Terms", font=BOLD, size=9)
        py = top + 26
        for ln in pay_lines:
            draw_text(c, 85, py, ln, size=9)
            py += 12
        content_bottom = py
    else:
        content_bottom = top + 8

    if notes:
        notes_top = content_bottom + 10
        note_lines: list[str] = []
        for para in notes.split("\n"):
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
        content_bottom = notes_top + note_h

    sig_y = content_bottom + 24
    # Keep signature above footer
    if sig_y + 110 > CONTENT_BOTTOM:
        sig_y = max(content_bottom + 16, CONTENT_BOTTOM - 110)

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
            Y(min(sig_y + 90, CONTENT_BOTTOM - 20)),
            X(83),
            37 * SY,
            mask="auto",
        )
    draw_text(c, 117, sig_y + 100, data.get("signatory", "Nilantha De Silva"), size=10)


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

    _draw_page_chrome(c, labels, number, continued=False)

    customer = data.get("customer") or {}
    cust_lines = [customer.get("name", ""), *customer.get("address_lines", [])]
    cust_lines = [ln for ln in cust_lines if ln]
    deliver = data.get("deliver_to") or []
    if not deliver and data.get("site"):
        deliver = [data["site"]]
    attention = data.get("attention") or []
    att_text = ", ".join(attention) if attention else ""
    duration = data.get("duration") or ""
    date_str = data.get("date") or default_invoice_date()

    left_x, right_x = 85, 380
    cell_w_l, cell_w_r = 280, 269
    top = 175

    cust_wrapped = _wrap_info_lines(cust_lines or [""], cell_w_l)
    deliver_wrapped = _wrap_info_lines(deliver or [""], cell_w_l)
    att_wrapped = _wrap_info_lines([att_text] if att_text else [""], cell_w_r)

    # Right column: Date + Duration stacked; left Invoice To matches their combined height
    date_h = duration_h = 24
    row1_h = max(_info_cell_height(cust_wrapped, min_h=48), date_h + duration_h)
    row2_h = max(
        _info_cell_height(deliver_wrapped, min_h=36),
        _info_cell_height(att_wrapped, min_h=36),
    )

    _draw_info_cell(c, left_x, top, cell_w_l, row1_h, labels["to_label"], cust_wrapped)
    _draw_info_cell(c, right_x, top, cell_w_r, date_h, "Date", [date_str])
    _draw_info_cell(
        c, right_x, top + date_h, cell_w_r, row1_h - date_h, "Duration", [duration]
    )

    top2 = top + row1_h
    _draw_info_cell(c, left_x, top2, cell_w_l, row2_h, "Deliver To", deliver_wrapped)
    _draw_info_cell(c, right_x, top2, cell_w_r, row2_h, "Attention", att_wrapped)

    top = _draw_table_header(c, top2 + row2_h + 12)

    items = data.get("items") or []
    desc_width = X(COLS["desc"][1] - COLS["desc"][0] - 12)
    layouts = [_prepare_item_layout(item, desc_width) for item in items]

    for i, layout in enumerate(layouts, start=1):
        # Leave room on last item page for totals if few items remain — but
        # primary rule: don't draw a row past CONTENT_BOTTOM.
        if top + layout["row_h"] > CONTENT_BOTTOM:
            top = _new_continuation_page(c, labels, number)
        top = _draw_item_row(c, top, i, layout)

    transportation = float(data.get("transportation") or 0)
    discount = float(data.get("discount") or 0)
    advance = float(data.get("advance") or 0)
    subtotal = compute_subtotal(items)
    grand = subtotal + transportation - discount - advance

    # Closing section needs space; otherwise start a fresh page
    if top + CLOSING_RESERVE > CONTENT_BOTTOM:
        c.showPage()
        _draw_page_chrome(c, labels, number, continued=True)
        top = CONT_TABLE_TOP

    _draw_closing(
        c,
        top,
        data=data,
        doc_type=doc_type,
        subtotal=subtotal,
        transportation=transportation,
        discount=discount,
        advance=advance,
        grand=grand,
    )

    c.showPage()
    c.save()
    return buf.getvalue()


# Alias used by newer call sites
generate_document = generate_invoice
