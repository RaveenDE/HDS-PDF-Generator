"""LLM-based invoice/quotation parsing + pydantic validation (OpenAI or Anthropic)."""
from __future__ import annotations

import json
import re
from typing import Any, Literal

import requests
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from config import get_secret, env
from logging_util import error, info, warning

SYSTEM_PROMPT = """You extract invoice or quotation data from informal WhatsApp messages (English, Sinhala, or mixed).
Return ONLY a single JSON object — no markdown, no code fences, no commentary.

Schema:
{
  "doc_type": "invoice" | "quotation",
  "customer": {"name": string, "address_lines": string[]},
  "deliver_to": string[],
  "attention": string[],
  "subject": string | null,
  "duration": string | null,
  "items": [{
    "description": string,
    "details": string[],
    "unit": string,
    "qty": number,
    "rate": number,
    "total": number | null
  }],
  "date": string | null,
  "transportation": number,
  "discount": number,
  "payment_terms": string | null,
  "notes": string | null,
  "missing": string[]
}

Rules:
- doc_type: "quotation" if the user says quotation/quote/උපුටා; otherwise "invoice" (including when they say invoice or omit the word).
- qty and rate must be positive numbers. Prefer integer qty when clear.
- unit defaults to "No.s" if omitted.
- total is optional; omit or null unless the user gave an explicit line total.
- date: use DD/MM/YYYY only if the user specified a date; otherwise null.
- deliver_to: delivery address lines ("deliver to", "delivery", "site"). Empty array if unknown.
- subject: short job title for "Quotation for" / "Invoice for" (e.g. "SS Work"); null if unknown.
- duration: e.g. "15 Days" if mentioned; null otherwise.
- attention: list of people names after "attention" / "attn" / similar.
- transportation and discount: non-negative numbers; use 0 if not mentioned.
- payment_terms / notes: only if the user stated them; otherwise null.
- Items with sub-lines: the line that has qty/rate (e.g. "7 door panels at 50000") is description
  (title only — strip qty/rate from description). Following lines until the next priced item are
  details[] bullet specs (e.g. "50mm dia SS Pipe Top Railing & Verticals"). Do not put details
  into description. details may be [].
- If customer name or at least one item (description + qty + rate) is missing, put short labels
  in "missing" (e.g. ["customer.name", "items"]) and still fill what you can.
- Phrases like "6 x 9615" or "6 @ 75000" or "7 door panels at 50000" mean qty + rate.
- Do not invent customers or line items that were not mentioned.
"""


class CustomerModel(BaseModel):
    name: str = ""
    address_lines: list[str] = Field(default_factory=list)


class ItemModel(BaseModel):
    description: str
    details: list[str] = Field(default_factory=list)
    unit: str = "No.s"
    qty: float
    rate: float
    total: float | None = None

    @field_validator("qty", "rate")
    @classmethod
    def positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("qty and rate must be positive")
        return v

    @field_validator("description")
    @classmethod
    def non_empty_desc(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("description required")
        return v.strip()

    @field_validator("details")
    @classmethod
    def clean_details(cls, v: list[str]) -> list[str]:
        return [d.strip() for d in v if d and str(d).strip()]


class ParsedInvoice(BaseModel):
    doc_type: Literal["invoice", "quotation"] = "invoice"
    customer: CustomerModel = Field(default_factory=CustomerModel)
    deliver_to: list[str] = Field(default_factory=list)
    attention: list[str] = Field(default_factory=list)
    subject: str | None = None
    duration: str | None = None
    items: list[ItemModel] = Field(default_factory=list)
    date: str | None = None
    transportation: float = 0
    discount: float = 0
    payment_terms: str | None = None
    notes: str | None = None
    missing: list[str] = Field(default_factory=list)
    # Back-compat: older payloads used site
    site: str | None = None

    @field_validator("transportation", "discount")
    @classmethod
    def non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError("transportation and discount must be >= 0")
        return v

    @model_validator(mode="after")
    def compute_missing(self) -> ParsedInvoice:
        # Map legacy site into deliver_to when needed
        if self.site and not self.deliver_to:
            self.deliver_to = [self.site]

        missing: list[str] = list(self.missing)
        if not (self.customer.name or "").strip():
            if "customer.name" not in missing and "customer" not in missing:
                missing.append("customer.name")
        if not self.items:
            if "items" not in missing:
                missing.append("items")
        seen: set[str] = set()
        deduped: list[str] = []
        for m in missing:
            if m not in seen:
                seen.add(m)
                deduped.append(m)
        self.missing = deduped
        return self

    def is_complete(self) -> bool:
        return not self.missing and bool(self.customer.name.strip()) and bool(self.items)

    def to_invoice_dict(self) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for it in self.items:
            d: dict[str, Any] = {
                "description": it.description,
                "details": list(it.details),
                "unit": it.unit or "No.s",
                "qty": int(it.qty) if float(it.qty).is_integer() else it.qty,
                "rate": it.rate,
            }
            if it.total is not None:
                d["total"] = it.total
            items.append(d)
        return {
            "doc_type": self.doc_type,
            "customer": {
                "name": self.customer.name.strip(),
                "address_lines": list(self.customer.address_lines),
            },
            "deliver_to": list(self.deliver_to),
            "attention": list(self.attention),
            "subject": self.subject or "",
            "duration": self.duration or "",
            "items": items,
            "date": self.date,
            "transportation": self.transportation,
            "discount": self.discount,
            "payment_terms": self.payment_terms,
            "notes": self.notes,
        }


def strip_code_fences(text: str) -> str:
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text, re.IGNORECASE)
    if fence:
        return fence.group(1).strip()
    return text


def validate_parsed(data: dict[str, Any]) -> ParsedInvoice:
    return ParsedInvoice.model_validate(data)


def merge_partial(existing: dict[str, Any] | None, new: ParsedInvoice) -> ParsedInvoice:
    """Merge a new parse onto stored conversation state."""
    base = existing or {}
    cust = base.get("customer") or {}
    merged_customer = {
        "name": new.customer.name.strip() or cust.get("name", ""),
        "address_lines": new.customer.address_lines
        or cust.get("address_lines")
        or [],
    }
    attention = new.attention or base.get("attention") or []
    deliver_to = new.deliver_to or base.get("deliver_to") or []
    if not deliver_to and base.get("site"):
        deliver_to = [base["site"]]
    items = [i.model_dump() for i in new.items] if new.items else base.get("items") or []
    date = new.date or base.get("date")
    subject = new.subject if new.subject is not None else base.get("subject")
    duration = new.duration if new.duration is not None else base.get("duration")
    doc_type = new.doc_type or base.get("doc_type") or "invoice"
    transportation = new.transportation if new.transportation else base.get("transportation", 0)
    discount = new.discount if new.discount else base.get("discount", 0)
    payment_terms = new.payment_terms if new.payment_terms is not None else base.get("payment_terms")
    notes = new.notes if new.notes is not None else base.get("notes")
    return validate_parsed(
        {
            "doc_type": doc_type,
            "customer": merged_customer,
            "attention": attention,
            "deliver_to": deliver_to,
            "subject": subject,
            "duration": duration,
            "items": items,
            "date": date,
            "transportation": transportation or 0,
            "discount": discount or 0,
            "payment_terms": payment_terms,
            "notes": notes,
            "missing": [],
        }
    )


def missing_prompt(parsed: ParsedInvoice) -> str:
    labels = {
        "customer.name": "customer / company name (Invoice To / Quote To)",
        "customer": "customer / company name (Invoice To / Quote To)",
        "items": "line items (description, quantity, and rate)",
    }
    parts = [labels.get(m, m) for m in parsed.missing]
    if not parts:
        parts = ["required document details"]
    joined = ", ".join(parts)
    return (
        f"I still need: {joined}.\n"
        "Example (invoice): Invoice DIMO Elevators, Colombo, deliver to Site A, "
        "attention Mr. Hassan, for door jambs, 6 door jamb installation at 75000, "
        "transport 5000, discount 2000\n"
        "Example (quotation with specs):\n"
        "Quotation DIMO Elevators, Colombo, deliver to Site A\n"
        "7 door panels at 50000\n"
        "50mm dia SS Pipe Top Railing & Verticals\n"
        "10mm dia SS Cable & fixing accessory's\n"
        "discount 20000"
    )


def _build_user_content(user_text: str, prior_state: dict[str, Any] | None) -> str:
    if prior_state:
        return (
            f"Previous partial document JSON:\n{json.dumps(prior_state)}\n\n"
            f"New WhatsApp message to merge:\n{user_text}"
        )
    return user_text


def _parse_model_text(text: str) -> ParsedInvoice:
    raw = strip_code_fences(text)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        warning("parser_json_decode_failed", preview=raw[:200])
        raise ValueError("Model did not return valid JSON") from exc
    try:
        parsed = validate_parsed(data)
    except ValidationError as exc:
        warning("parser_validation_failed", errors=str(exc))
        raise
    info(
        "parser_ok",
        item_count=len(parsed.items),
        missing=parsed.missing,
        doc_type=parsed.doc_type,
    )
    return parsed


def call_openai(user_text: str, *, prior_state: dict[str, Any] | None = None) -> ParsedInvoice:
    api_key = get_secret("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not configured")

    model = env("OPENAI_MODEL", "gpt-4o-mini")
    user_content = _build_user_content(user_text, prior_state)

    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": 1024,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
    }
    if env("OPENAI_JSON_MODE", "true").lower() in ("1", "true", "yes"):
        payload["response_format"] = {"type": "json_object"}

    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=45,
    )
    if resp.status_code >= 400:
        error("openai_api_error", status_code=resp.status_code, body=resp.text[:300])
        raise RuntimeError(f"OpenAI API HTTP {resp.status_code}")

    body = resp.json()
    choices = body.get("choices") or []
    if not choices:
        raise ValueError("OpenAI returned no choices")
    text = (choices[0].get("message") or {}).get("content") or ""
    return _parse_model_text(text)


def call_anthropic(user_text: str, *, prior_state: dict[str, Any] | None = None) -> ParsedInvoice:
    api_key = get_secret("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")

    model = env("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
    user_content = _build_user_content(user_text, prior_state)

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 1024,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_content}],
        },
        timeout=45,
    )
    if resp.status_code >= 400:
        error("anthropic_api_error", status_code=resp.status_code, body=resp.text[:300])
        raise RuntimeError(f"Anthropic API HTTP {resp.status_code}")

    body = resp.json()
    chunks = body.get("content") or []
    text = "".join(c.get("text", "") for c in chunks if c.get("type") == "text")
    return _parse_model_text(text)


def call_llm(user_text: str, *, prior_state: dict[str, Any] | None = None) -> ParsedInvoice:
    """Parse document text using the configured LLM provider (default: OpenAI)."""
    provider = env("LLM_PROVIDER", "openai").strip().lower()
    if provider == "anthropic":
        return call_anthropic(user_text, prior_state=prior_state)
    if provider == "openai":
        return call_openai(user_text, prior_state=prior_state)
    raise RuntimeError(f"Unsupported LLM_PROVIDER: {provider}")
