"""Load secrets from Secrets Manager / SSM SecureString; cache at cold start."""
from __future__ import annotations

import json
import os

import boto3

_cache: dict[str, str] | None = None

SECRET_KEYS = (
    "WHATSAPP_TOKEN",
    "APP_SECRET",
    "VERIFY_TOKEN",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
)


def _load_from_secrets_manager(secret_arn: str) -> dict[str, str]:
    client = boto3.client("secretsmanager")
    resp = client.get_secret_value(SecretId=secret_arn)
    raw = resp.get("SecretString") or ""
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Secret must be a JSON object")
    return {k: str(v) for k, v in data.items()}


def _load_from_ssm(prefix: str) -> dict[str, str]:
    """Load SecureString params under prefix, e.g. /whatsapp-invoice/."""
    client = boto3.client("ssm")
    result: dict[str, str] = {}
    paginator = client.get_paginator("get_parameters_by_path")
    for page in paginator.paginate(Path=prefix, Recursive=True, WithDecryption=True):
        for param in page.get("Parameters", []):
            name = param["Name"].rstrip("/").split("/")[-1]
            result[name] = param["Value"]
    return result


def get_secrets() -> dict[str, str]:
    """Return WHATSAPP_TOKEN, APP_SECRET, VERIFY_TOKEN, OPENAI_API_KEY, ANTHROPIC_API_KEY.

    Resolution order:
    1. Cached values from a previous cold-start load
    2. SECRET_ARN (Secrets Manager JSON)
    3. SSM_PARAM_PREFIX (SecureString path)
    4. Plain environment variables (local/dev only)
    """
    global _cache
    if _cache is not None:
        return _cache

    loaded: dict[str, str] = {}
    secret_arn = os.environ.get("SECRET_ARN", "").strip()
    ssm_prefix = os.environ.get("SSM_PARAM_PREFIX", "").strip()

    if secret_arn:
        loaded.update(_load_from_secrets_manager(secret_arn))
    elif ssm_prefix:
        loaded.update(_load_from_ssm(ssm_prefix))

    out: dict[str, str] = {}
    for key in SECRET_KEYS:
        out[key] = loaded.get(key) or os.environ.get(key, "")
    _cache = out
    return out


def get_secret(name: str) -> str:
    return get_secrets().get(name, "")


def clear_secrets_cache() -> None:
    """Test helper."""
    global _cache
    _cache = None


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value
