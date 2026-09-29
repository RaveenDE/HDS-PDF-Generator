#!/usr/bin/env python3
"""Sign a sample WhatsApp webhook payload and POST it to a Function URL."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
import urllib.request

SAMPLE = {
    "object": "whatsapp_business_account",
    "entry": [
        {
            "id": "WABA_ID",
            "changes": [
                {
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {
                            "display_phone_number": "15550001111",
                            "phone_number_id": "PHONE_NUMBER_ID",
                        },
                        "contacts": [
                            {"profile": {"name": "Test"}, "wa_id": "94771234567"}
                        ],
                        "messages": [
                            {
                                "from": "94771234567",
                                "id": "wamid.SIMULATE001",
                                "timestamp": "1710000000",
                                "type": "text",
                                "text": {
                                    "body": (
                                        "Invoice DIMO Elevators, Colombo, attention Mr. Hassan, "
                                        "6 door jamb installation at 75000, "
                                        "6 door jamb modification at 9615"
                                    )
                                },
                            }
                        ],
                    },
                    "field": "messages",
                }
            ],
        }
    ],
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Webhook Function URL")
    parser.add_argument(
        "--app-secret",
        required=True,
        help="Meta App Secret used for X-Hub-Signature-256",
    )
    parser.add_argument(
        "--payload",
        help="Path to JSON payload (default: built-in sample)",
    )
    parser.add_argument(
        "--from",
        dest="from_phone",
        default="94771234567",
        help="Sender phone (digits, no +)",
    )
    args = parser.parse_args()

    if args.payload:
        with open(args.payload, encoding="utf-8") as f:
            payload = json.load(f)
    else:
        payload = SAMPLE
        payload["entry"][0]["changes"][0]["value"]["messages"][0]["from"] = args.from_phone
        payload["entry"][0]["changes"][0]["value"]["contacts"][0]["wa_id"] = args.from_phone

    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    digest = hmac.new(args.app_secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        args.url,
        data=raw,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": f"sha256={digest}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode()
            print(f"HTTP {resp.status}: {body}")
            return 0
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read().decode()}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
