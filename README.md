# WhatsApp Invoice PDF Generator
#
# Sensible defaults chosen for first deploy:
# - Secrets live in one Secrets Manager JSON secret (SECRET_ARN).
# - Webhook uses Lambda Function URL with AuthType NONE (Meta requirement).
# - Invoice numbers start at 2578 (INVOICE_START) for company key "default".
# - Date format on PDFs is DD/MM/YYYY to match the existing template.
# - Default line unit is "No.s".
# - Invoice parsing uses OpenAI by default (LLM_PROVIDER=openai, OPENAI_MODEL=gpt-4o-mini).
# - Set LLM_PROVIDER=anthropic to use Claude instead (ANTHROPIC_MODEL).
# - Graph API version defaults to v21.0.
# - Existing invoice-lambda/ tree is left untouched; the live code is under src/.

## Architecture

```
WhatsApp Cloud API
        │
        ▼
Lambda Function URL  (webhook, AuthType NONE)
        │  verify signature → SQS
        ▼
SQS (+ DLQ, maxReceiveCount=3)
        ▼
Lambda worker  (parse → DynamoDB → PDF → S3 → WhatsApp media/document)
```

## Prerequisites

- AWS CLI + SAM CLI (`sam`)
- Python 3.12
- Meta WhatsApp Cloud API app with a phone number, WABA, and App Secret
- OpenAI API key (or Anthropic if you set `LlmProvider=anthropic`)
- Permanent **System User** WhatsApp token (not a temporary User token)

## Load secrets

Create a Secrets Manager secret whose value is JSON:

```json
{
  "WHATSAPP_TOKEN": "EAAB...",
  "APP_SECRET": "your-meta-app-secret",
  "VERIFY_TOKEN": "pick-a-long-random-string",
  "OPENAI_API_KEY": "sk-..."
}
```

Optional (only if `LlmProvider=anthropic`):

```json
{
  "ANTHROPIC_API_KEY": "sk-ant-..."
}
```

```bash
aws secretsmanager create-secret \
  --name whatsapp-invoice/secrets \
  --secret-string file://secrets.json
```

Note the ARN for deploy. Alternatively set `SSM_PARAM_PREFIX` (e.g. `/whatsapp-invoice/`) with SecureString parameters named `WHATSAPP_TOKEN`, `APP_SECRET`, `VERIFY_TOKEN`, `OPENAI_API_KEY`.

### OpenAI API key

1. Go to [platform.openai.com](https://platform.openai.com/) → **API keys** → **Create new secret key**.
2. Copy the key (`sk-…`) into `OPENAI_API_KEY` in your Secrets Manager JSON (or local env for tests).
3. On deploy, leave **`LlmProvider`** as `openai` (default) and optionally change **`OpenAIModel`** (default `gpt-4o-mini`).

## Deploy

```bash
sam build
sam deploy --guided
```

When prompted, set:

| Parameter | Example |
|-----------|---------|
| `SecretArn` | `arn:aws:secretsmanager:...:secret:whatsapp-invoice/secrets-xxxxx` |
| `PhoneNumberId` | Graph **Phone number ID** (not the WABA ID) |
| `WabaId` | WhatsApp Business Account ID |
| `AllowedSenders` | `94771234567,94770001111` (E.164 **without** `+`) |
| `InvoiceStart` | `2578` |

Stack outputs include `WebhookUrl` — that is your Meta Callback URL.

## Meta / WhatsApp configuration

1. Meta Developer Console → your app → **WhatsApp → Configuration**.
2. **Callback URL**: paste `WebhookUrl` (must be HTTPS Function URL).
3. **Verify token**: same value as `VERIFY_TOKEN` in the secret.
4. Click **Verify and save**. Meta sends a GET with `hub.mode`, `hub.verify_token`, `hub.challenge`. The Lambda returns the challenge as **plain text**.
5. Subscribe to the webhook field **`messages`**.
6. Subscribe the app to the WABA:

```bash
curl -X POST "https://graph.facebook.com/v21.0/${WABA_ID}/subscribed_apps" \
  -H "Authorization: Bearer ${WHATSAPP_TOKEN}"
```

7. Switch the app to **Live** when ready for production traffic. In Development mode, Meta only delivers webhooks for allowlisted testers/test numbers.

## Local tests

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Unix:    source .venv/bin/activate
pip install -r requirements.txt
pytest -q
```

## Simulate a webhook

```bash
python scripts/simulate_webhook.py https://xxxxxxxx.lambda-url.region.on.aws/ \
  --app-secret "$APP_SECRET" \
  --from 94771234567
```

## Commands (in WhatsApp chat)

| Message | Effect |
|---------|--------|
| invoice details… | Parse → PDF → send document |
| `help` | Usage example |
| `cancel` | Clear partial draft |
| `last invoice` | Resend most recent PDF from S3 |

## Environment reference

**Secrets** (Secrets Manager / SSM / env): `WHATSAPP_TOKEN`, `APP_SECRET`, `VERIFY_TOKEN`, `OPENAI_API_KEY` (and `ANTHROPIC_API_KEY` if using Anthropic)

**Plain env**: `LLM_PROVIDER` (`openai` | `anthropic`), `OPENAI_MODEL`, `ANTHROPIC_MODEL`, `PHONE_NUMBER_ID`, `WABA_ID`, `GRAPH_API_VERSION`, `ALLOWED_SENDERS`, `INVOICE_START`, `BUCKET_NAME`, `TABLE_NAME`, `QUEUE_URL`, `SECRET_ARN`

---

## TROUBLESHOOTING

### Messages not arriving / no reply

Work through this list in order.

1. **App still in Development mode**  
   Meta only sends real customer webhooks once the app is **Live**. In Development, only numbers added as testers receive/send via the test number.

2. **`messages` field not subscribed**  
   WhatsApp → Configuration → Webhook fields → ensure **messages** is subscribed.

3. **App not subscribed to the WABA**  
   Run `POST /{WABA_ID}/subscribed_apps` with your System User token (see above).

4. **Wrong Function URL auth type**  
   Must be **NONE**. IAM auth blocks Meta’s unsigned GET/POST. This template sets `AuthType: NONE`.

5. **GET verification failing**  
   The handler must return HTTP 200 with `hub.challenge` as **plain text**, not JSON. Check CloudWatch for `webhook_verify_failed`.

6. **Signature failures (401)**  
   Causes: wrong `APP_SECRET`; body re-serialized before HMAC; Function URL `isBase64Encoded` not decoded. The webhook hashes the **raw** body bytes. Re-test with `scripts/simulate_webhook.py`.

7. **Expired temporary token**  
   User tokens expire in hours/days. Create a **System User**, grant WhatsApp permissions, generate a permanent token, store it as `WHATSAPP_TOKEN`.

8. **PHONE_NUMBER_ID vs WABA_ID mix-up**  
   Media and messages APIs use **Phone number ID**. `WABA_ID` is only for account-level calls like `subscribed_apps`.

9. **Recipient not on the test-number list**  
   While in Development, add the customer WhatsApp number under API Setup → To.

10. **Sender not on the allowlist**  
    Worker only invoices numbers in `ALLOWED_SENDERS` (digits, no `+`). Others get a polite refusal. Check the parameter value in the stack.

11. **24-hour window / template rules**  
    Outside the customer-care window, free-form replies fail with Graph error **131047**. The worker logs `whatsapp_outside_24h_window`. You must use an approved template message to re-open the window (not implemented here — start the chat from the customer side).

12. **Read CloudWatch logs**

```bash
# Replace names from stack outputs
aws logs tail /aws/lambda/STACK-WebhookFunction-XXXX --follow --format short
aws logs tail /aws/lambda/STACK-WorkerFunction-XXXX --follow --format short
```

13. **Inspect the DLQ** (poison messages after 3 receives)

```bash
aws sqs receive-message \
  --queue-url "$(aws cloudformation describe-stacks --stack-name YOUR_STACK --query "Stacks[0].Outputs[?OutputKey=='WebhookDLQUrl'].OutputValue" --output text)" \
  --max-number-of-messages 5 \
  --visibility-timeout 30 \
  --attribute-names All \
  --message-attribute-names All
```

Also check the CloudWatch alarm on DLQ depth.

### Useful log fields

Worker logs are JSON. Correlate with `wamid`. Phone numbers are masked to the last 4 digits. Tokens and secrets are never logged.

## Project layout

```
src/webhook/handler.py      # GET verify + POST signature + SQS
src/worker/handler.py       # SQS consumer
src/worker/whatsapp.py      # Graph API client
src/worker/parser.py        # Claude + pydantic
src/worker/store.py         # DynamoDB
src/worker/invoice.py       # ReportLab PDF (existing layout)
src/worker/assets/          # header, footer, signature
tests/                      # pytest + moto + responses
events/                     # sample GET/POST payloads
scripts/simulate_webhook.py
template.yaml
```

## Manual steps after deploy

1. Create/populate the Secrets Manager secret and pass `SecretArn` to SAM.
2. Set Meta Callback URL + verify token; subscribe to `messages`.
3. `POST /{WABA_ID}/subscribed_apps`.
4. Put your phone in `AllowedSenders`.
5. Switch the Meta app to Live when ready.
6. Send a WhatsApp text to the business number and confirm a PDF reply.
