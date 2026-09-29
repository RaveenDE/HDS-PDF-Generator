"""WhatsApp Cloud API (Graph) client."""
from __future__ import annotations

import os
import time
from typing import Any

import requests

from config import get_secret
from logging_util import error, info, warning

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_RETRIES = 4
OUTSIDE_WINDOW_CODE = 131047


class WhatsAppError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_body: dict[str, Any] | None = None,
        retryable: bool = False,
        outside_window: bool = False,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.error_body = error_body or {}
        self.retryable = retryable
        self.outside_window = outside_window


def _base_url() -> str:
    version = os.environ.get("GRAPH_API_VERSION", "v21.0")
    return f"https://graph.facebook.com/{version}"


def _phone_number_id() -> str:
    return os.environ["PHONE_NUMBER_ID"]


def _token() -> str:
    return get_secret("WHATSAPP_TOKEN")


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token()}"}


def _parse_error(resp: requests.Response) -> WhatsAppError:
    try:
        body = resp.json()
    except Exception:
        body = {"raw": resp.text[:500]}
    err = body.get("error", body) if isinstance(body, dict) else {"message": str(body)}
    code = err.get("code") if isinstance(err, dict) else None
    outside = code == OUTSIDE_WINDOW_CODE
    retryable = resp.status_code in RETRYABLE_STATUS
    msg = f"Graph API HTTP {resp.status_code}: {err}"
    if outside:
        error("whatsapp_outside_24h_window", status_code=resp.status_code, error=err)
    else:
        error("whatsapp_api_error", status_code=resp.status_code, error=err)
    return WhatsAppError(
        msg,
        status_code=resp.status_code,
        error_body=err if isinstance(err, dict) else {"error": err},
        retryable=retryable,
        outside_window=outside,
    )


def _request_with_retry(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    data: Any = None,
    files: Any = None,
    timeout: int = 30,
) -> dict[str, Any]:
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        resp = requests.request(
            method,
            url,
            headers=headers,
            json=json_body,
            data=data,
            files=files,
            timeout=timeout,
        )
        if resp.status_code < 400:
            try:
                return resp.json() if resp.content else {}
            except Exception:
                return {}

        # Non-retryable 4xx (except 429)
        if resp.status_code < 500 and resp.status_code != 429:
            raise _parse_error(resp)

        wa_err = _parse_error(resp)
        last_exc = wa_err
        if not wa_err.retryable or attempt == MAX_RETRIES - 1:
            raise wa_err
        sleep_s = 2 ** attempt
        warning("whatsapp_retry", attempt=attempt + 1, sleep_s=sleep_s, status_code=resp.status_code)
        time.sleep(sleep_s)

    raise last_exc or WhatsAppError("WhatsApp request failed")


def send_text(to: str, body: str, *, wamid: str | None = None) -> dict[str, Any]:
    url = f"{_base_url()}/{_phone_number_id()}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": body},
    }
    info("whatsapp_send_text", to_masked=_mask(to), wamid=wamid)
    return _request_with_retry("POST", url, headers={**_headers(), "Content-Type": "application/json"}, json_body=payload)


def mark_read(message_id: str) -> None:
    """Mark a message as read. Non-fatal on failure."""
    url = f"{_base_url()}/{_phone_number_id()}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
    }
    try:
        _request_with_retry(
            "POST",
            url,
            headers={**_headers(), "Content-Type": "application/json"},
            json_body=payload,
        )
    except Exception as exc:
        warning("mark_read_failed", wamid=message_id, error=str(exc))


def upload_media(pdf_bytes: bytes, filename: str) -> str:
    """Upload PDF; return media id."""
    url = f"{_base_url()}/{_phone_number_id()}/media"
    files = {
        "file": (filename, pdf_bytes, "application/pdf"),
    }
    data = {
        "messaging_product": "whatsapp",
        "type": "application/pdf",
    }
    info("whatsapp_upload_media", filename=filename, size=len(pdf_bytes))
    result = _request_with_retry("POST", url, headers=_headers(), data=data, files=files)
    media_id = result.get("id")
    if not media_id:
        raise WhatsAppError(f"Media upload missing id: {result}")
    return str(media_id)


def send_document(
    to: str,
    media_id: str,
    *,
    filename: str,
    caption: str,
    wamid: str | None = None,
) -> dict[str, Any]:
    url = f"{_base_url()}/{_phone_number_id()}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "document",
        "document": {
            "id": media_id,
            "filename": filename,
            "caption": caption,
        },
    }
    info("whatsapp_send_document", to_masked=_mask(to), filename=filename, wamid=wamid)
    return _request_with_retry(
        "POST",
        url,
        headers={**_headers(), "Content-Type": "application/json"},
        json_body=payload,
    )


def _mask(phone: str) -> str:
    from logging_util import mask_phone

    return mask_phone(phone)
