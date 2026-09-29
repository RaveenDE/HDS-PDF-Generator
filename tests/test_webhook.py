"""Webhook Lambda unit tests."""
from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import json
import os
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

ROOT = Path(__file__).resolve().parents[1]
WEBHOOK_PATH = ROOT / "src" / "webhook" / "handler.py"


def _load_webhook():
    spec = importlib.util.spec_from_file_location("webhook_handler_mod", WEBHOOK_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["webhook_handler_mod"] = mod
    spec.loader.exec_module(mod)
    return mod


webhook_handler = _load_webhook()

APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "my-verify-token"


@pytest.fixture(autouse=True)
def env_secrets(monkeypatch):
    monkeypatch.setenv("APP_SECRET", APP_SECRET)
    monkeypatch.setenv("VERIFY_TOKEN", VERIFY_TOKEN)
    monkeypatch.setenv("WHATSAPP_TOKEN", "tok")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    monkeypatch.delenv("SECRET_ARN", raising=False)
    monkeypatch.delenv("SSM_PARAM_PREFIX", raising=False)
    webhook_handler.clear_secrets_cache()
    yield
    webhook_handler.clear_secrets_cache()


def _sign(body: bytes) -> str:
    digest = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _post_event(body: bytes, *, signature: str | None = None, b64: bool = False) -> dict:
    if b64:
        encoded = base64.b64encode(body).decode()
        return {
            "requestContext": {"http": {"method": "POST"}},
            "headers": {"x-hub-signature-256": signature or _sign(body)},
            "body": encoded,
            "isBase64Encoded": True,
        }
    return {
        "requestContext": {"http": {"method": "POST"}},
        "headers": {"X-Hub-Signature-256": signature or _sign(body)},
        "body": body.decode("utf-8"),
        "isBase64Encoded": False,
    }


@mock_aws
def test_valid_signature_enqueues():
    sqs = boto3.client("sqs", region_name="us-east-1")
    q = sqs.create_queue(QueueName="webhook-q")
    os.environ["QUEUE_URL"] = q["QueueUrl"]
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"

    payload = {"object": "whatsapp_business_account", "entry": []}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    resp = webhook_handler.handler(_post_event(raw), None)
    assert resp["statusCode"] == 200
    msgs = sqs.receive_message(QueueUrl=q["QueueUrl"], MaxNumberOfMessages=1)
    assert "Messages" in msgs
    assert json.loads(msgs["Messages"][0]["Body"]) == payload


@mock_aws
def test_invalid_signature_401():
    sqs = boto3.client("sqs", region_name="us-east-1")
    q = sqs.create_queue(QueueName="webhook-q2")
    os.environ["QUEUE_URL"] = q["QueueUrl"]
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"

    raw = b'{"object":"whatsapp_business_account"}'
    resp = webhook_handler.handler(_post_event(raw, signature="sha256=deadbeef"), None)
    assert resp["statusCode"] == 401


def test_get_challenge_ok():
    event = {
        "requestContext": {"http": {"method": "GET"}},
        "queryStringParameters": {
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "challenge-abc",
        },
    }
    resp = webhook_handler.handler(event, None)
    assert resp["statusCode"] == 200
    assert resp["body"] == "challenge-abc"
    assert "text/plain" in resp["headers"]["Content-Type"]


def test_get_challenge_bad_token():
    event = {
        "httpMethod": "GET",
        "queryStringParameters": {
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong",
            "hub.challenge": "challenge-abc",
        },
    }
    resp = webhook_handler.handler(event, None)
    assert resp["statusCode"] == 403


@mock_aws
def test_base64_body_signature():
    sqs = boto3.client("sqs", region_name="us-east-1")
    q = sqs.create_queue(QueueName="webhook-q3")
    os.environ["QUEUE_URL"] = q["QueueUrl"]
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"

    raw = b'{"object":"whatsapp_business_account","entry":[{"id":"1"}]}'
    resp = webhook_handler.handler(_post_event(raw, b64=True), None)
    assert resp["statusCode"] == 200
    msgs = sqs.receive_message(QueueUrl=q["QueueUrl"], MaxNumberOfMessages=1)
    assert json.loads(msgs["Messages"][0]["Body"])["entry"][0]["id"] == "1"
