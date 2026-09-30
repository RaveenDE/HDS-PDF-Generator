"""Worker Lambda end-to-end tests with moto + responses."""
from __future__ import annotations

import json
import os

import boto3
import pytest
import responses
from moto import mock_aws

import config
import handler as worker
import store
import whatsapp


PHONE = "94771234567"
GRAPH = "https://graph.facebook.com/v21.0"
PHONE_ID = "1234567890"


def _wa_payload(text: str, *, wamid: str = "wamid.TEST1", msg_type: str = "text", from_phone: str = PHONE) -> dict:
    msg: dict = {
        "from": from_phone,
        "id": wamid,
        "timestamp": "1710000000",
        "type": msg_type,
    }
    if msg_type == "text":
        msg["text"] = {"body": text}
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": PHONE_ID},
                            "contacts": [{"wa_id": from_phone}],
                            "messages": [msg],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }


def _sqs_event(payload: dict) -> dict:
    return {
        "Records": [
            {
                "messageId": "sqs-1",
                "body": json.dumps(payload),
            }
        ]
    }


@pytest.fixture
def aws_env(monkeypatch):
    with mock_aws():
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        monkeypatch.setenv("PHONE_NUMBER_ID", PHONE_ID)
        monkeypatch.setenv("GRAPH_API_VERSION", "v21.0")
        monkeypatch.setenv("ALLOWED_SENDERS", PHONE)
        monkeypatch.setenv("INVOICE_START", "2578")
        monkeypatch.setenv("QUOTATION_START", "26929")
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("OPENAI_MODEL", "gpt-test")
        monkeypatch.setenv("WHATSAPP_TOKEN", "wa-token")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("APP_SECRET", "secret")
        monkeypatch.setenv("VERIFY_TOKEN", "verify")
        monkeypatch.delenv("SECRET_ARN", raising=False)
        config.clear_secrets_cache()

        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="invoices",
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="invoice-bucket")

        monkeypatch.setenv("TABLE_NAME", "invoices")
        monkeypatch.setenv("BUCKET_NAME", "invoice-bucket")
        yield
        config.clear_secrets_cache()


def _mock_openai(payload: dict):
    responses.add(
        responses.POST,
        "https://api.openai.com/v1/chat/completions",
        json={
            "choices": [{"message": {"role": "assistant", "content": json.dumps(payload)}}]
        },
        status=200,
    )


def _mock_whatsapp_happy():
    responses.add(
        responses.POST,
        f"{GRAPH}/{PHONE_ID}/messages",
        json={"messages": [{"id": "out1"}]},
        status=200,
    )
    responses.add(
        responses.POST,
        f"{GRAPH}/{PHONE_ID}/media",
        json={"id": "media123"},
        status=200,
    )
    # send_document + mark_read + generating text also hit /messages
    responses.add(
        responses.POST,
        f"{GRAPH}/{PHONE_ID}/messages",
        json={"messages": [{"id": "out2"}]},
        status=200,
    )
    responses.add(
        responses.POST,
        f"{GRAPH}/{PHONE_ID}/messages",
        json={"messages": [{"id": "out3"}]},
        status=200,
    )


@responses.activate
def test_text_message_end_to_end(aws_env):
    claude = {
        "doc_type": "invoice",
        "customer": {"name": "DIMO Elevators", "address_lines": ["Colombo"]},
        "attention": ["Mr. Hassan"],
        "deliver_to": ["Colombo"],
        "subject": "Door jambs",
        "items": [
            {"description": "Door jamb installation", "unit": "No.s", "qty": 6, "rate": 75000},
            {"description": "Door jamb modification", "unit": "No.s", "qty": 6, "rate": 9615},
        ],
        "date": "14/09/2026",
        "transportation": 5000,
        "discount": 1000,
        "missing": [],
    }
    _mock_openai(claude)
    responses.add(
        responses.POST,
        f"{GRAPH}/{PHONE_ID}/messages",
        json={"messages": [{"id": "m"}]},
        status=200,
    )
    responses.add(
        responses.POST,
        f"{GRAPH}/{PHONE_ID}/media",
        json={"id": "media123"},
        status=200,
    )

    result = worker.handler(_sqs_event(_wa_payload("Invoice DIMO...")), None)
    assert result["batchItemFailures"] == []

    s3 = boto3.client("s3", region_name="us-east-1")
    obj = s3.get_object(Bucket="invoice-bucket", Key="invoices/2578.pdf")
    pdf = obj["Body"].read()
    assert pdf.startswith(b"%PDF")

    media_calls = [c for c in responses.calls if c.request.url.endswith("/media")]
    assert media_calls
    # Caption should include grand total: (450000+57690)+5000-1000 = 511690
    # 6*75000=450000, 6*9615=57690
    doc_calls = [
        json.loads(c.request.body.decode())
        for c in responses.calls
        if c.request.url.endswith("/messages") and c.request.body
        and b'"type": "document"' in c.request.body
    ]
    assert doc_calls
    assert "511,690.00" in doc_calls[0]["document"]["caption"]


@responses.activate
def test_quotation_end_to_end(aws_env):
    payload = {
        "doc_type": "quotation",
        "customer": {"name": "Mr. Annaz", "address_lines": []},
        "deliver_to": ["Alvis Place"],
        "attention": [],
        "subject": "SS Work",
        "duration": "15 Days",
        "items": [
            {"description": "SS Floor Grating", "unit": "L Ft", "qty": 36, "rate": 11000},
        ],
        "date": "29/09/2026",
        "transportation": 0,
        "discount": 0,
        "missing": [],
    }
    _mock_openai(payload)
    responses.add(
        responses.POST,
        f"{GRAPH}/{PHONE_ID}/messages",
        json={"messages": [{"id": "m"}]},
        status=200,
    )
    responses.add(
        responses.POST,
        f"{GRAPH}/{PHONE_ID}/media",
        json={"id": "media123"},
        status=200,
    )
    result = worker.handler(
        _sqs_event(_wa_payload("Quotation Mr Annaz...", wamid="wamid.QUOTE1")),
        None,
    )
    assert result["batchItemFailures"] == []
    s3 = boto3.client("s3", region_name="us-east-1")
    obj = s3.get_object(Bucket="invoice-bucket", Key="quotations/26929.pdf")
    assert obj["Body"].read().startswith(b"%PDF")
    doc_calls = [
        json.loads(c.request.body.decode())
        for c in responses.calls
        if c.request.url.endswith("/messages") and c.request.body
        and b'"type": "document"' in c.request.body
    ]
    assert doc_calls
    assert doc_calls[0]["document"]["filename"] == "Quotation-26929.pdf"

@responses.activate
def test_duplicate_wamid_ignored(aws_env):
    claude = {
        "customer": {"name": "Acme", "address_lines": []},
        "attention": [],
        "site": None,
        "items": [{"description": "Item", "unit": "No.s", "qty": 1, "rate": 100}],
        "date": None,
        "missing": [],
    }
    _mock_openai(claude)
    responses.add(
        responses.POST,
        f"{GRAPH}/{PHONE_ID}/messages",
        json={"messages": [{"id": "m"}]},
        status=200,
    )
    responses.add(
        responses.POST,
        f"{GRAPH}/{PHONE_ID}/media",
        json={"id": "media123"},
        status=200,
    )

    payload = _wa_payload("Invoice Acme 1 item at 100", wamid="wamid.DUP")
    assert worker.handler(_sqs_event(payload), None)["batchItemFailures"] == []
    # Second delivery of same wamid should no-op (no new invoice)
    assert worker.handler(_sqs_event(payload), None)["batchItemFailures"] == []

    s3 = boto3.client("s3", region_name="us-east-1")
    listed = s3.list_objects_v2(Bucket="invoice-bucket", Prefix="invoices/")
    assert listed.get("KeyCount", 0) == 1


@responses.activate
def test_non_allowlisted_sender_refused(aws_env, monkeypatch):
    monkeypatch.setenv("ALLOWED_SENDERS", "94111111111")
    responses.add(
        responses.POST,
        f"{GRAPH}/{PHONE_ID}/messages",
        json={"messages": [{"id": "m"}]},
        status=200,
    )
    result = worker.handler(
        _sqs_event(_wa_payload("Invoice X", from_phone="94770000000", wamid="wamid.REF")),
        None,
    )
    assert result["batchItemFailures"] == []
    bodies = [
        json.loads(c.request.body.decode())
        for c in responses.calls
        if c.request.url.endswith("/messages") and c.request.body
    ]
    assert any("not authorised" in b.get("text", {}).get("body", "").lower() for b in bodies)


@responses.activate
def test_missing_info_follow_up(aws_env):
    _mock_openai(
        {
            "customer": {"name": "DIMO Elevators", "address_lines": ["Colombo"]},
            "attention": [],
            "site": None,
            "items": [],
            "date": None,
            "missing": ["items"],
        }
    )
    responses.add(
        responses.POST,
        f"{GRAPH}/{PHONE_ID}/messages",
        json={"messages": [{"id": "m"}]},
        status=200,
    )
    result = worker.handler(
        _sqs_event(_wa_payload("Invoice DIMO Elevators Colombo", wamid="wamid.MISS")),
        None,
    )
    assert result["batchItemFailures"] == []
    state = store.get_conversation_state(PHONE)
    assert state is not None
    assert state["customer"]["name"] == "DIMO Elevators"

    bodies = [
        json.loads(c.request.body.decode())
        for c in responses.calls
        if c.request.url.endswith("/messages") and c.request.body
    ]
    assert any("still need" in b.get("text", {}).get("body", "").lower() for b in bodies)


@responses.activate
def test_graph_api_4xx_no_retry_poison(aws_env):
    claude = {
        "customer": {"name": "Acme", "address_lines": []},
        "attention": [],
        "site": None,
        "items": [{"description": "Item", "qty": 1, "rate": 50}],
        "date": None,
        "missing": [],
    }
    _mock_openai(claude)
    # mark_read + generating succeed
    responses.add(
        responses.POST,
        f"{GRAPH}/{PHONE_ID}/messages",
        json={"messages": [{"id": "m"}]},
        status=200,
    )
    responses.add(
        responses.POST,
        f"{GRAPH}/{PHONE_ID}/messages",
        json={"messages": [{"id": "m"}]},
        status=200,
    )
    # media 400
    responses.add(
        responses.POST,
        f"{GRAPH}/{PHONE_ID}/media",
        json={"error": {"message": "bad request", "code": 100}},
        status=400,
    )
    # friendly error text
    responses.add(
        responses.POST,
        f"{GRAPH}/{PHONE_ID}/messages",
        json={"messages": [{"id": "m"}]},
        status=200,
    )

    result = worker.handler(
        _sqs_event(_wa_payload("Invoice Acme 1 item at 50", wamid="wamid.4XX")),
        None,
    )
    # Non-retryable: should not report batch failure
    assert result["batchItemFailures"] == []


@responses.activate
def test_graph_api_5xx_retries_then_fails(aws_env, monkeypatch):
    monkeypatch.setattr(whatsapp.time, "sleep", lambda *_: None)
    claude = {
        "customer": {"name": "Acme", "address_lines": []},
        "attention": [],
        "site": None,
        "items": [{"description": "Item", "qty": 1, "rate": 50}],
        "date": None,
        "missing": [],
    }
    _mock_openai(claude)
    responses.add(
        responses.POST,
        f"{GRAPH}/{PHONE_ID}/messages",
        json={"messages": [{"id": "m"}]},
        status=200,
    )
    responses.add(
        responses.POST,
        f"{GRAPH}/{PHONE_ID}/messages",
        json={"messages": [{"id": "m"}]},
        status=200,
    )
    for _ in range(whatsapp.MAX_RETRIES):
        responses.add(
            responses.POST,
            f"{GRAPH}/{PHONE_ID}/media",
            json={"error": {"message": "server", "code": 2}},
            status=500,
        )
    responses.add(
        responses.POST,
        f"{GRAPH}/{PHONE_ID}/messages",
        json={"messages": [{"id": "m"}]},
        status=200,
    )

    result = worker.handler(
        _sqs_event(_wa_payload("Invoice Acme 1 item at 50", wamid="wamid.5XX")),
        None,
    )
    assert result["batchItemFailures"] == [{"itemIdentifier": "sqs-1"}]


def test_generate_invoice_bytes(aws_env):
    from invoice import generate_invoice

    pdf = generate_invoice(
        {
            "doc_type": "quotation",
            "invoice_no": "26929",
            "date": "29/09/2026",
            "duration": "15 Days",
            "customer": {"name": "Mr. Annaz", "address_lines": []},
            "deliver_to": ["Alvis Place"],
            "attention": [],
            "subject": "SS Work",
            "items": [
                {
                    "description": "S/S Hand Rail with 10 SS Cable",
                    "details": [
                        "50mm dia SS Pipe Top Railing & Verticals",
                        "10mm dia SS Cable & fixing accessory's",
                    ],
                    "unit": "Lft",
                    "qty": 27,
                    "rate": 14000,
                },
                {
                    "description": "Door Jamb Modification",
                    "details": ["10mm Thick Tempered Glass"],
                    "unit": "No.s",
                    "qty": 6,
                    "rate": 9615,
                    "total": 57690,
                },
            ],
            "transportation": 5000,
            "discount": 2000,
        }
    )
    assert pdf.startswith(b"%PDF")


def test_many_items_multipage(aws_env):
    from invoice import generate_invoice

    items = []
    for n in range(1, 15):
        items.append(
            {
                "description": f"Item {n}",
                "details": [
                    "50mm dia SS Pipe Top Railing & Verticals",
                    "10mm dia SS Cable & fixing accessory's",
                ],
                "unit": "No.s",
                "qty": 1,
                "rate": 1000,
            }
        )
    pdf = generate_invoice(
        {
            "doc_type": "quotation",
            "invoice_no": "26938",
            "date": "30/09/2026",
            "customer": {"name": "DIMO Elevators", "address_lines": ["Colombo"]},
            "deliver_to": ["Site A"],
            "attention": ["Mr. Hassan"],
            "items": items,
            "discount": 20000,
        }
    )
    assert pdf.startswith(b"%PDF")
    # Multi-page: PDF should contain more than one /Type /Page
    assert pdf.count(b"/Type /Page") >= 2 or pdf.count(b"/Type/Page") >= 2
