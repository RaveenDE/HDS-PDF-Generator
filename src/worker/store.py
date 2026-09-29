"""DynamoDB helpers: message dedupe, invoice counter, conversation state, metadata."""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any

import boto3

from logging_util import info, warning

PK = "pk"
SK = "sk"
TTL_ATTR = "ttl"

DEDUPE_TTL_SECONDS = 7 * 24 * 3600
STATE_TTL_SECONDS = 3600
COMPANY_DEFAULT = "default"


def _table():
    name = os.environ["TABLE_NAME"]
    return boto3.resource("dynamodb").Table(name)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ttl(seconds: int) -> int:
    return int(time.time()) + seconds


# ---------------------------------------------------------------------------
# Idempotency (WhatsApp message id)
# ---------------------------------------------------------------------------

def try_claim_message(wamid: str) -> bool:
    """Conditional PutItem on wamid. Returns True if this is the first claim."""
    table = _table()
    item = {
        PK: f"MSG#{wamid}",
        SK: "DEDUPE",
        "wamid": wamid,
        "created_at": _now_iso(),
        TTL_ATTR: _ttl(DEDUPE_TTL_SECONDS),
        "entity_type": "dedupe",
    }
    from botocore.exceptions import ClientError

    try:
        table.put_item(
            Item=item,
            ConditionExpression=f"attribute_not_exists({PK})",
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            warning("duplicate_wamid_ignored", wamid=wamid)
            return False
        raise


def release_message_claim(wamid: str) -> None:
    """Delete dedupe row so SQS can safely retry after a failed attempt."""
    table = _table()
    table.delete_item(Key={PK: f"MSG#{wamid}", SK: "DEDUPE"})
    info("dedupe_claim_released", wamid=wamid)


# ---------------------------------------------------------------------------
# Conversation state (partial invoice drafts keyed by phone)
# ---------------------------------------------------------------------------

def get_conversation_state(phone: str) -> dict[str, Any] | None:
    table = _table()
    resp = table.get_item(Key={PK: f"STATE#{phone}", SK: "DRAFT"})
    item = resp.get("Item")
    if not item:
        return None
    return item.get("state")


def put_conversation_state(phone: str, state: dict[str, Any]) -> None:
    table = _table()
    table.put_item(
        Item={
            PK: f"STATE#{phone}",
            SK: "DRAFT",
            "state": state,
            "updated_at": _now_iso(),
            TTL_ATTR: _ttl(STATE_TTL_SECONDS),
            "entity_type": "state",
        }
    )


def clear_conversation_state(phone: str) -> None:
    table = _table()
    table.delete_item(Key={PK: f"STATE#{phone}", SK: "DRAFT"})


# ---------------------------------------------------------------------------
# Invoice number counter
# ---------------------------------------------------------------------------

def next_document_number(doc_type: str = "invoice", company: str = COMPANY_DEFAULT) -> int:
    """Atomic ADD counter per doc_type (invoice | quotation)."""
    from botocore.exceptions import ClientError

    table = _table()
    kind = "quotation" if doc_type == "quotation" else "invoice"
    if kind == "quotation":
        start = int(os.environ.get("QUOTATION_START", "26929"))
    else:
        start = int(os.environ.get("INVOICE_START", "2578"))
    pk = f"COUNTER#{company}#{kind}"

    try:
        table.put_item(
            Item={
                PK: pk,
                SK: "COUNTER",
                "value": start - 1,
                "entity_type": "counter",
                "doc_type": kind,
            },
            ConditionExpression=f"attribute_not_exists({PK})",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise

    resp = table.update_item(
        Key={PK: pk, SK: "COUNTER"},
        UpdateExpression="ADD #v :one",
        ExpressionAttributeNames={"#v": "value"},
        ExpressionAttributeValues={":one": 1},
        ReturnValues="UPDATED_NEW",
    )
    return int(resp["Attributes"]["value"])


def next_invoice_number(company: str = COMPANY_DEFAULT) -> int:
    """Back-compat wrapper — invoice counter only."""
    return next_document_number("invoice", company=company)


# ---------------------------------------------------------------------------
# Invoice metadata
# ---------------------------------------------------------------------------

def save_invoice_metadata(
    *,
    invoice_no: int | str,
    customer: dict[str, Any],
    total: float,
    s3_key: str,
    created_by: str,
    doc_type: str = "invoice",
    company: str = COMPANY_DEFAULT,
) -> None:
    table = _table()
    number = str(invoice_no)
    kind = "quotation" if doc_type == "quotation" else "invoice"
    created_at = _now_iso()
    table.put_item(
        Item={
            PK: f"DOC#{kind}#{number}",
            SK: "META",
            "invoice_no": number,
            "doc_type": kind,
            "customer": customer,
            "total": str(total),
            "s3_key": s3_key,
            "created_at": created_at,
            "created_by": created_by,
            "company": company,
            "entity_type": kind,
        }
    )
    # Latest pointer per sender for "last invoice" / resend
    table.put_item(
        Item={
            PK: f"SENDER#{created_by}",
            SK: "LAST",
            "invoice_no": number,
            "doc_type": kind,
            "s3_key": s3_key,
            "total": str(total),
            "created_at": created_at,
            "entity_type": "last_invoice",
        }
    )
    info("invoice_metadata_saved", invoice_no=number, doc_type=kind)


def get_last_invoice(phone: str) -> dict[str, Any] | None:
    table = _table()
    resp = table.get_item(Key={PK: f"SENDER#{phone}", SK: "LAST"})
    return resp.get("Item")
