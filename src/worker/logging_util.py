"""Structured JSON logging helpers. Never log secrets or full phone numbers."""
from __future__ import annotations

import json
import logging
import os
from typing import Any

_configured = False


def mask_phone(phone: str | None) -> str:
    if not phone:
        return "****"
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) <= 4:
        return "****"
    return f"***{digits[-4:]}"


def configure_logging() -> None:
    global _configured
    if _configured:
        return
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(message)s",
        force=True,
    )
    _configured = True


def log(level: int, message: str, *, wamid: str | None = None, **fields: Any) -> None:
    configure_logging()
    payload: dict[str, Any] = {"msg": message, **fields}
    if wamid:
        payload["wamid"] = wamid
    logging.log(level, json.dumps(payload, default=str))


def info(message: str, **fields: Any) -> None:
    log(logging.INFO, message, **fields)


def warning(message: str, **fields: Any) -> None:
    log(logging.WARNING, message, **fields)


def error(message: str, **fields: Any) -> None:
    log(logging.ERROR, message, **fields)
