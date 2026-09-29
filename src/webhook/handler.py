"""
WhatsApp Cloud API webhook Lambda.

GET  — Meta verification challenge (plain-text hub.challenge).
POST — verify X-Hub-Signature-256 on the raw body, enqueue to SQS, return 200 fast.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from typing import Any
from urllib.parse import parse_qs

import boto3

# Secrets are loaded the same way as the worker (Secrets Manager / SSM / env).
_secrets_cache: dict[str, str] | None = None


def _load_secrets() -> dict[str, str]:
    global _secrets_cache
    if _secrets_cache is not None:
        return _secrets_cache

    loaded: dict[str, str] = {}
    secret_arn = os.environ.get("SECRET_ARN", "").strip()
    ssm_prefix = os.environ.get("SSM_PARAM_PREFIX", "").strip()
    if secret_arn:
        client = boto3.client("secretsmanager")
        resp = client.get_secret_value(SecretId=secret_arn)
        data = json.loads(resp.get("SecretString") or "{}")
        loaded.update({k: str(v) for k, v in data.items()})
    elif ssm_prefix:
        client = boto3.client("ssm")
        paginator = client.get_paginator("get_parameters_by_path")
        for page in paginator.paginate(Path=ssm_prefix, Recursive=True, WithDecryption=True):
            for param in page.get("Parameters", []):
                name = param["Name"].rstrip("/").split("/")[-1]
                loaded[name] = param["Value"]

    keys = ("WHATSAPP_TOKEN", "APP_SECRET", "VERIFY_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")
    _secrets_cache = {k: loaded.get(k) or os.environ.get(k, "") for k in keys}
    return _secrets_cache


def clear_secrets_cache() -> None:
    global _secrets_cache
    _secrets_cache = None


def _log(msg: str, **fields: Any) -> None:
    print(json.dumps({"msg": msg, **fields}, default=str))


def _query_params(event: dict[str, Any]) -> dict[str, str]:
    """Normalize API Gateway v2 and Function URL query shapes."""
    params = event.get("queryStringParameters") or {}
    if params:
        return {k: (v if v is not None else "") for k, v in params.items()}

    # Function URL / some proxies put the raw query on rawQueryString
    raw = event.get("rawQueryString") or ""
    if raw:
        parsed = parse_qs(raw, keep_blank_values=True)
        return {k: (v[0] if v else "") for k, v in parsed.items()}
    return {}


def _header(event: dict[str, Any], name: str) -> str | None:
    headers = event.get("headers") or {}
    target = name.lower()
    for k, v in headers.items():
        if k.lower() == target:
            return v
    return None


def _raw_body(event: dict[str, Any]) -> bytes:
    """Return the exact body bytes Meta signed. Do not re-serialize JSON."""
    body = event.get("body")
    if body is None:
        return b""
    if event.get("isBase64Encoded"):
        if isinstance(body, bytes):
            return base64.b64decode(body)
        return base64.b64decode(body.encode("utf-8"))
    if isinstance(body, bytes):
        return body
    return body.encode("utf-8")


def _response(status: int, body: str, *, content_type: str = "text/plain") -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": content_type},
        "body": body,
    }


def _handle_get(event: dict[str, Any]) -> dict[str, Any]:
    params = _query_params(event)
    mode = params.get("hub.mode") or params.get("hub_mode") or ""
    token = params.get("hub.verify_token") or params.get("hub_verify_token") or ""
    challenge = params.get("hub.challenge") or params.get("hub_challenge") or ""

    expected = _load_secrets().get("VERIFY_TOKEN", "")
    if mode == "subscribe" and token and hmac.compare_digest(token, expected):
        _log("webhook_verified")
        return _response(200, challenge, content_type="text/plain")
    _log("webhook_verify_failed", mode=mode)
    return _response(403, "Forbidden")


def _verify_signature(raw: bytes, signature_header: str | None, app_secret: str) -> bool:
    if not signature_header or not app_secret:
        return False
    if not signature_header.startswith("sha256="):
        return False
    provided = signature_header[7:]
    digest = hmac.new(app_secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, provided)


def _handle_post(event: dict[str, Any]) -> dict[str, Any]:
    raw = _raw_body(event)
    sig = _header(event, "X-Hub-Signature-256")
    app_secret = _load_secrets().get("APP_SECRET", "")

    if not _verify_signature(raw, sig, app_secret):
        _log("signature_mismatch")
        return _response(401, "Unauthorized")

    queue_url = os.environ.get("QUEUE_URL", "")
    if not queue_url:
        _log("missing_queue_url")
        return _response(500, "Server misconfigured")

    # Enqueue the exact raw payload string (UTF-8 decode for SQS MessageBody)
    try:
        body_str = raw.decode("utf-8")
    except UnicodeDecodeError:
        body_str = raw.decode("utf-8", errors="replace")

    boto3.client("sqs").send_message(QueueUrl=queue_url, MessageBody=body_str)
    _log("enqueued", bytes=len(raw))
    return _response(200, "EVENT_RECEIVED")


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    method = (
        (event.get("requestContext") or {}).get("http", {}).get("method")
        or event.get("httpMethod")
        or ""
    ).upper()

    if method == "GET":
        return _handle_get(event)
    if method == "POST":
        return _handle_post(event)
    return _response(405, "Method Not Allowed")
