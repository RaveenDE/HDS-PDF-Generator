"""
SQS-triggered worker: parse WhatsApp text -> invoice/quotation PDF -> send document.
"""
from __future__ import annotations

import json
import os
from typing import Any

import boto3

import store
import whatsapp
from config import clear_secrets_cache  # noqa: F401 — re-export for tests
from invoice import compute_grand_total, default_invoice_date, generate_invoice
from logging_util import error, info, mask_phone, warning
from parser import (
    call_llm,
    merge_partial,
    missing_prompt,
)

HELP_TEXT = (
    "Send an invoice or quotation request like:\n"
    "Quotation DIMO Elevators, Colombo, deliver to Site A, attention Mr. Hassan\n"
    "7 door panels at 50000\n"
    "50mm dia SS Pipe Top Railing & Verticals\n"
    "10mm dia SS Cable & fixing accessory's\n"
    "6 door jambs at 9615\n"
    "50mm dia SS Pipe...\n"
    "discount 20000\n"
    "Payment Term - Pay 50% advance...\n"
    "Notes - Valid only for 3 days\n\n"
    "Commands: help | cancel | last invoice"
)

REFUSAL_TEXT = (
    "Sorry, this number is not authorised to create invoices. "
    "Please contact the business owner."
)

GENERATING_TEXT = "Generating your document..."
SOMETHING_WRONG = "Sorry, something went wrong generating your document. Please try again shortly."
NON_TEXT_HELP = "Please send a text message with the invoice or quotation details. Type help for an example."


def _allowed_senders() -> set[str]:
    raw = os.environ.get("ALLOWED_SENDERS", "")
    senders = {p.strip().lstrip("+") for p in raw.split(",") if p.strip()}
    if not senders or senders == {"*"}:
        return set()
    return senders


def _normalize_phone(phone: str) -> str:
    return "".join(c for c in phone if c.isdigit())


def _s3():
    return boto3.client("s3")


def _doc_label(doc_type: str) -> str:
    return "Quotation" if doc_type == "quotation" else "Invoice"


def _extract_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten entry[].changes[].value.messages[]; ignore statuses."""
    out: list[dict[str, Any]] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            for msg in value.get("messages") or []:
                contacts = value.get("contacts") or []
                meta = {
                    "message": msg,
                    "contacts": contacts,
                    "metadata": value.get("metadata") or {},
                }
                out.append(meta)
    return out


def _handle_command(text: str, phone: str, wamid: str) -> bool:
    cmd = text.strip().lower()
    if cmd in ("help", "hi", "hello", "start"):
        whatsapp.send_text(phone, HELP_TEXT, wamid=wamid)
        return True
    if cmd == "cancel":
        store.clear_conversation_state(phone)
        whatsapp.send_text(phone, "Cancelled. Send a new request when ready.", wamid=wamid)
        return True
    if cmd in ("last invoice", "last", "resend", "last quotation", "last quote"):
        last = store.get_last_invoice(phone)
        if not last:
            whatsapp.send_text(phone, "No previous document found for this chat.", wamid=wamid)
            return True
        bucket = os.environ["BUCKET_NAME"]
        key = last["s3_key"]
        obj = _s3().get_object(Bucket=bucket, Key=key)
        pdf = obj["Body"].read()
        number = last["invoice_no"]
        total = last.get("total", "")
        doc_type = last.get("doc_type") or "invoice"
        label = _doc_label(doc_type)
        filename = f"{label}-{number}.pdf"
        media_id = whatsapp.upload_media(pdf, filename)
        whatsapp.send_document(
            phone,
            media_id,
            filename=filename,
            caption=f"{label} {number} - Total Rs. {total}",
            wamid=wamid,
        )
        return True
    return False


def _create_and_send_invoice(parsed_dict: dict[str, Any], phone: str, wamid: str) -> None:
    doc_type = (parsed_dict.get("doc_type") or "invoice").lower()
    if doc_type not in ("invoice", "quotation"):
        doc_type = "invoice"

    doc_no = store.next_document_number(doc_type)
    date_str = parsed_dict.get("date") or default_invoice_date()
    data = {
        "doc_type": doc_type,
        "invoice_no": str(doc_no),
        "date": date_str,
        "duration": parsed_dict.get("duration") or "",
        "customer": parsed_dict["customer"],
        "deliver_to": parsed_dict.get("deliver_to") or [],
        "attention": parsed_dict.get("attention") or [],
        "subject": parsed_dict.get("subject") or "",
        "items": parsed_dict["items"],
        "transportation": float(parsed_dict.get("transportation") or 0),
        "discount": float(parsed_dict.get("discount") or 0),
        "advance": float(parsed_dict.get("advance") or 0),
        "advance_note": parsed_dict.get("advance_note"),
        "payment_terms": parsed_dict.get("payment_terms"),
        "notes": parsed_dict.get("notes"),
    }
    pdf = generate_invoice(data)
    if not pdf.startswith(b"%PDF"):
        raise RuntimeError("generate_invoice did not return a PDF")

    total = compute_grand_total(
        data["items"],
        transportation=data["transportation"],
        discount=data["discount"],
        advance=data["advance"],
    )
    bucket = os.environ["BUCKET_NAME"]
    folder = "quotations" if doc_type == "quotation" else "invoices"
    key = f"{folder}/{doc_no}.pdf"
    _s3().put_object(
        Bucket=bucket,
        Key=key,
        Body=pdf,
        ContentType="application/pdf",
        ServerSideEncryption="AES256",
    )
    store.save_invoice_metadata(
        invoice_no=doc_no,
        customer=data["customer"],
        total=total,
        s3_key=key,
        created_by=phone,
        doc_type=doc_type,
    )
    store.clear_conversation_state(phone)

    label = _doc_label(doc_type)
    filename = f"{label}-{doc_no}.pdf"
    amount_label = "Balance" if data["advance"] else "Total"
    caption = f"{label} {doc_no} - {amount_label} Rs. {total:,.2f}"
    media_id = whatsapp.upload_media(pdf, filename)
    whatsapp.send_document(phone, media_id, filename=filename, caption=caption, wamid=wamid)
    info(
        "document_sent",
        invoice_no=str(doc_no),
        doc_type=doc_type,
        wamid=wamid,
        to_masked=mask_phone(phone),
    )


def process_message(msg_wrap: dict[str, Any]) -> None:
    msg = msg_wrap["message"]
    wamid = msg.get("id", "")
    phone = _normalize_phone(msg.get("from", ""))
    msg_type = msg.get("type", "")

    info("process_message", wamid=wamid, from_masked=mask_phone(phone), msg_type=msg_type)

    if not wamid or not phone:
        warning("skip_incomplete_message", wamid=wamid)
        return

    if not store.try_claim_message(wamid):
        return

    try:
        _process_claimed_message(msg, phone, wamid, msg_type)
    except Exception:
        store.release_message_claim(wamid)
        raise


def _process_claimed_message(msg: dict[str, Any], phone: str, wamid: str, msg_type: str) -> None:
    allowed = _allowed_senders()
    if allowed and phone not in allowed:
        whatsapp.send_text(phone, REFUSAL_TEXT, wamid=wamid)
        return

    whatsapp.mark_read(wamid)

    if msg_type != "text":
        whatsapp.send_text(phone, NON_TEXT_HELP, wamid=wamid)
        return

    text = ((msg.get("text") or {}).get("body") or "").strip()
    if not text:
        whatsapp.send_text(phone, NON_TEXT_HELP, wamid=wamid)
        return

    if _handle_command(text, phone, wamid):
        return

    whatsapp.send_text(phone, GENERATING_TEXT, wamid=wamid)

    prior = store.get_conversation_state(phone)
    try:
        parsed = call_llm(text, prior_state=prior)
        if prior:
            parsed = merge_partial(prior, parsed)
    except Exception as exc:
        error("parse_failed", wamid=wamid, error=str(exc))
        whatsapp.send_text(
            phone,
            "I couldn't understand that request. Type help for an example.",
            wamid=wamid,
        )
        return

    if not parsed.is_complete():
        store.put_conversation_state(phone, parsed.to_invoice_dict())
        whatsapp.send_text(phone, missing_prompt(parsed), wamid=wamid)
        return

    try:
        _create_and_send_invoice(parsed.to_invoice_dict(), phone, wamid)
    except whatsapp.WhatsAppError as exc:
        error("send_failed", wamid=wamid, error=str(exc), retryable=exc.retryable)
        whatsapp.send_text(phone, SOMETHING_WRONG, wamid=wamid)
        if exc.retryable:
            raise
        return
    except Exception as exc:
        error("invoice_pipeline_failed", wamid=wamid, error=str(exc))
        try:
            whatsapp.send_text(phone, SOMETHING_WRONG, wamid=wamid)
        except Exception:
            pass
        raise


def process_payload(payload: dict[str, Any]) -> None:
    messages = _extract_messages(payload)
    if not messages:
        info("no_messages_in_payload")
        return
    for wrap in messages:
        process_message(wrap)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """SQS handler with ReportBatchItemFailures (batch size 1)."""
    failures: list[dict[str, str]] = []
    for record in event.get("Records") or []:
        msg_id = record.get("messageId", "")
        try:
            body = record.get("body") or "{}"
            payload = json.loads(body)
            if "Message" in payload and "entry" not in payload:
                payload = json.loads(payload["Message"])
            process_payload(payload)
        except Exception as exc:
            error("record_failed", sqs_message_id=msg_id, error=str(exc))
            failures.append({"itemIdentifier": msg_id})
    return {"batchItemFailures": failures}
