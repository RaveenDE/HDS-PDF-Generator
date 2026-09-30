"""Parser validation and realistic message fixtures."""
from __future__ import annotations

import json

import pytest
import responses
from pydantic import ValidationError

from invoice import compute_grand_total, compute_subtotal
from parser import (
    ParsedInvoice,
    call_anthropic,
    call_llm,
    call_openai,
    merge_partial,
    strip_code_fences,
    validate_parsed,
)


EXAMPLES = [
    {
        "name": "dimo_elevators",
        "claude_json": {
            "doc_type": "invoice",
            "customer": {"name": "DIMO Elevators", "address_lines": ["Colombo"]},
            "attention": ["Mr. Hassan"],
            "deliver_to": ["Colombo"],
            "subject": "Door jambs",
            "items": [
                {"description": "Door jamb installation", "unit": "No.s", "qty": 6, "rate": 75000},
                {"description": "Door jamb modification", "unit": "No.s", "qty": 6, "rate": 9615},
            ],
            "date": None,
            "transportation": 0,
            "discount": 0,
            "missing": [],
        },
    },
    {
        "name": "x_rate_style",
        "claude_json": {
            "doc_type": "invoice",
            "customer": {"name": "ABC Traders", "address_lines": ["Nugegoda"]},
            "attention": [],
            "deliver_to": [],
            "items": [
                {"description": "Aluminium frame", "unit": "No.s", "qty": 4, "rate": 12500}
            ],
            "date": None,
            "missing": [],
        },
    },
    {
        "name": "sinhala_english_mixed",
        "claude_json": {
            "doc_type": "invoice",
            "customer": {"name": "Perera & Sons", "address_lines": ["Kandy"]},
            "attention": ["Mr. Silva"],
            "deliver_to": [],
            "items": [
                {"description": "Door repair", "unit": "No.s", "qty": 2, "rate": 15000}
            ],
            "date": None,
            "missing": [],
        },
    },
    {
        "name": "at_symbol_rate",
        "claude_json": {
            "doc_type": "invoice",
            "customer": {"name": "Metro Homes", "address_lines": ["Dehiwala"]},
            "attention": [],
            "items": [
                {"description": "Wall panels", "unit": "No.s", "qty": 10, "rate": 8500}
            ],
            "date": None,
            "missing": [],
        },
    },
    {
        "name": "quotation_with_transport_discount",
        "claude_json": {
            "doc_type": "quotation",
            "customer": {"name": "Mr. Annaz", "address_lines": []},
            "deliver_to": ["Alvis Place"],
            "attention": [],
            "subject": "SS Work",
            "duration": "15 Days",
            "items": [
                {"description": "SS Floor Grating", "unit": "L Ft", "qty": 36, "rate": 11000},
                {"description": "L angle frame", "unit": "L Ft", "qty": 36, "rate": 6000},
            ],
            "date": "29/09/2026",
            "transportation": 5000,
            "discount": 2000,
            "missing": [],
        },
    },
    {
        "name": "with_date_and_deliver",
        "claude_json": {
            "doc_type": "invoice",
            "customer": {"name": "Green Builders", "address_lines": ["Negombo"]},
            "attention": ["Mrs. Fernando"],
            "deliver_to": ["Site A"],
            "items": [
                {"description": "Shutter installation", "unit": "No.s", "qty": 3, "rate": 45000}
            ],
            "date": "20/09/2026",
            "missing": [],
        },
    },
    {
        "name": "missing_items",
        "claude_json": {
            "doc_type": "invoice",
            "customer": {"name": "DIMO Elevators", "address_lines": ["Colombo"]},
            "attention": [],
            "items": [],
            "date": None,
            "missing": ["items"],
        },
    },
]


@pytest.mark.parametrize("example", EXAMPLES, ids=[e["name"] for e in EXAMPLES])
def test_validate_realistic_examples(example):
    parsed = validate_parsed(example["claude_json"])
    assert isinstance(parsed, ParsedInvoice)
    if example["name"] == "missing_items":
        assert not parsed.is_complete()
        assert "items" in parsed.missing
    else:
        assert parsed.is_complete()
        assert parsed.items[0].qty > 0
        assert parsed.items[0].rate > 0


def test_legacy_site_maps_to_deliver_to():
    parsed = validate_parsed(
        {
            "customer": {"name": "X", "address_lines": []},
            "site": "Site A",
            "items": [{"description": "Y", "qty": 1, "rate": 100}],
        }
    )
    assert parsed.deliver_to == ["Site A"]


def test_reject_zero_qty():
    with pytest.raises(ValidationError):
        validate_parsed(
            {
                "customer": {"name": "X", "address_lines": []},
                "items": [{"description": "Y", "qty": 0, "rate": 100}],
            }
        )


def test_reject_negative_rate():
    with pytest.raises(ValidationError):
        validate_parsed(
            {
                "customer": {"name": "X", "address_lines": []},
                "items": [{"description": "Y", "qty": 1, "rate": -5}],
            }
        )


def test_reject_negative_discount():
    with pytest.raises(ValidationError):
        validate_parsed(
            {
                "customer": {"name": "X", "address_lines": []},
                "items": [{"description": "Y", "qty": 1, "rate": 100}],
                "discount": -10,
            }
        )


def test_strip_code_fences():
    raw = '```json\n{"customer": {"name": "A", "address_lines": []}, "items": []}\n```'
    assert strip_code_fences(raw).startswith("{")


def test_merge_partial_fills_items():
    prior = {
        "doc_type": "invoice",
        "customer": {"name": "DIMO Elevators", "address_lines": ["Colombo"]},
        "attention": [],
        "deliver_to": [],
        "items": [],
        "date": None,
    }
    new = validate_parsed(
        {
            "customer": {"name": "", "address_lines": []},
            "items": [{"description": "Door", "qty": 2, "rate": 1000}],
        }
    )
    merged = merge_partial(prior, new)
    assert merged.is_complete()
    assert merged.customer.name == "DIMO Elevators"
    assert len(merged.items) == 1


def test_item_with_details_bullets():
    parsed = validate_parsed(
        {
            "doc_type": "quotation",
            "customer": {"name": "DIMO Elevators", "address_lines": ["Colombo"]},
            "deliver_to": ["Site A"],
            "items": [
                {
                    "description": "door panels",
                    "details": [
                        "50mm dia SS Pipe Top Railing & Verticals",
                        "10mm dia SS Cable & fixing accessory's",
                    ],
                    "qty": 7,
                    "rate": 50000,
                }
            ],
            "discount": 20000,
            "missing": [],
        }
    )
    assert parsed.is_complete()
    assert parsed.items[0].description == "door panels"
    assert len(parsed.items[0].details) == 2
    d = parsed.to_invoice_dict()
    assert d["items"][0]["details"][0].startswith("50mm")


def test_pdf_renders_details(aws_env=None):
    from invoice import generate_invoice

    pdf = generate_invoice(
        {
            "doc_type": "quotation",
            "invoice_no": "26930",
            "date": "30/09/2026",
            "customer": {"name": "DIMO Elevators", "address_lines": ["Colombo"]},
            "deliver_to": ["Site A"],
            "attention": ["Mr. Hassan"],
            "items": [
                {
                    "description": "door panels",
                    "details": [
                        "50mm dia SS Pipe Top Railing & Verticals",
                        "10mm dia SS Cable & fixing accessory's",
                    ],
                    "unit": "No.s",
                    "qty": 7,
                    "rate": 50000,
                },
                {
                    "description": "door jambs",
                    "details": ["50mm dia SS Pipe Top Railing & Verticals"],
                    "unit": "No.s",
                    "qty": 6,
                    "rate": 9615,
                },
            ],
            "discount": 20000,
            "payment_terms": "Pay 50% advance first and the balance within ten days of delivery",
            "notes": "This Quotation will valid only for 3 days.",
        }
    )
    assert pdf.startswith(b"%PDF")

    items = [
        {"description": "A", "qty": 36, "rate": 11000},
        {"description": "B", "qty": 36, "rate": 6000},
    ]
    assert compute_subtotal(items) == 612_000
    assert compute_grand_total(items, transportation=5000, discount=2000) == 615_000
    # Explicit line total override (sample style)
    items2 = [
        {"description": "A", "qty": 36, "rate": 11000},
        {"description": "B", "qty": 36, "rate": 6000, "total": 210_000},
    ]
    assert compute_subtotal(items2) == 606_000
    assert compute_grand_total(items2, transportation=0, discount=0) == 606_000


@responses.activate
def test_call_openai_parses_json(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-test")
    from config import clear_secrets_cache

    clear_secrets_cache()

    payload = {
        "doc_type": "invoice",
        "customer": {"name": "Acme", "address_lines": ["Colombo"]},
        "attention": [],
        "deliver_to": [],
        "items": [{"description": "Widget", "unit": "No.s", "qty": 1, "rate": 100}],
        "date": None,
        "missing": [],
    }
    responses.add(
        responses.POST,
        "https://api.openai.com/v1/chat/completions",
        json={
            "choices": [{"message": {"role": "assistant", "content": json.dumps(payload)}}]
        },
        status=200,
    )
    parsed = call_openai("Invoice Acme Colombo 1 widget at 100")
    assert parsed.customer.name == "Acme"
    assert parsed.is_complete()

    monkeypatch.setenv("LLM_PROVIDER", "openai")
    clear_secrets_cache()
    parsed2 = call_llm("Invoice Acme Colombo 1 widget at 100")
    assert parsed2.customer.name == "Acme"


@responses.activate
def test_call_anthropic_parses_fenced_json(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-test")
    from config import clear_secrets_cache

    clear_secrets_cache()

    payload = {
        "doc_type": "quotation",
        "customer": {"name": "Acme", "address_lines": ["Colombo"]},
        "attention": [],
        "items": [{"description": "Widget", "unit": "No.s", "qty": 1, "rate": 100}],
        "date": None,
        "missing": [],
    }
    responses.add(
        responses.POST,
        "https://api.anthropic.com/v1/messages",
        json={
            "content": [{"type": "text", "text": f"```json\n{json.dumps(payload)}\n```"}]
        },
        status=200,
    )
    parsed = call_anthropic("Quotation Acme Colombo 1 widget at 100")
    assert parsed.customer.name == "Acme"
    assert parsed.doc_type == "quotation"
    assert parsed.is_complete()
