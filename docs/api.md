# API Design

Client apps use the API to sync contacts, check verification/subscription state, send transactional emails, and create/send campaigns.

All client API endpoints require authentication. Clients can have multiple named API keys for separate integrations. Datamailer stores only key hashes and displays each key's safe `dm_<prefix>` identifier for support and audit trails.

## Authentication

Request header:

```text
Authorization: Bearer <client-api-key>
```

The API key maps to one `client`. Any active, non-revoked key for an active client is accepted. Revoked keys fail immediately, and successful authentication updates the key's `last_used_at`.

Local demo data creates stable named keys for examples:

- `dtc-courses` / `Course platform transactional`: `dm_dtccourses_demo_transactional_email_key`
- `dtc-newsletter` / `Newsletter import/export`: `dm_dtcnews_demo_newsletter_import_export_key`
- `asl-platform` / `ASL platform transactional`: `dm_aslplatform_demo_transactional_email_key`

## Contact APIs

### Upsert Contact

```text
POST /api/contacts
```

Request:

```json
{
  "email": "person@example.com",
  "audience": "datatalks-club",
  "client": "dtc-courses",
  "tags": ["ml-zoomcamp", "lead"],
  "status": "subscribed",
  "verified": false
}
```

Behavior:

- Creates or updates the global contact.
- Creates or updates the audience/client subscription.
- Adds tags in the target audience.
- Does not automatically mark globally verified unless the client is trusted to assert verification.

### Contact Status / Verification Lookup

```text
GET /api/contacts/status?email=person@example.com&audience=datatalks-club&client=dtc-courses
```

Response:

```json
{
  "email": "person@example.com",
  "exists": true,
  "verified": true,
  "verified_at": "2026-05-24T12:00:00Z",
  "global_unsubscribed": false,
  "hard_bounced": false,
  "complained": false,
  "audience": {
    "slug": "datatalks-club",
    "subscribed": true,
    "status": "subscribed",
    "verified": true
  },
  "client": {
    "slug": "dtc-courses",
    "subscribed": true,
    "status": "subscribed"
  },
  "can_send_marketing": true,
  "can_send_transactional": true
}
```

Rules:

- `can_send_marketing` requires verified, subscribed, not globally unsubscribed, not hard-bounced, and not complained.
- `can_send_transactional` may allow unverified contacts for verification/password flows, but must block hard bounces and complaints.

### Subscribe

```text
POST /api/subscriptions/subscribe
```

Request:

```json
{
  "email": "person@example.com",
  "audience": "datatalks-club",
  "client": "dtc-newsletter",
  "tags": ["newsletter"]
}
```

Behavior:

- Creates a pending or subscribed subscription depending on verification policy.
- Sends a verification email if required.

### Unsubscribe

```text
POST /api/subscriptions/unsubscribe
```

Request:

```json
{
  "email": "person@example.com",
  "scope": "client",
  "audience": "datatalks-club",
  "client": "dtc-courses",
  "reason": "api_request"
}
```

Scopes:

- `client`: unsubscribe from one client.
- `audience`: unsubscribe from an entire audience.
- `global`: unsubscribe from all marketing email managed by Datamailer.

## Campaign APIs

### Create Campaign

```text
POST /api/campaigns
```

Request:

```json
{
  "audience": "datatalks-club",
  "client": "dtc-newsletter",
  "subject": "ML Zoomcamp starts soon",
  "preview_text": "Registration closes this week",
  "html_body": "<p>Hello...</p>",
  "text_body": "Hello...",
  "include_tags": ["ml-zoomcamp"],
  "exclude_tags": ["unsubscribed-import"]
}
```

### Queue Campaign

```text
POST /api/campaigns/{campaign_id}/queue
```

Behavior:

- Snapshots recipients into `campaign_recipients`.
- Marks skipped recipients with explicit reasons.
- Enqueues send jobs.

### Campaign Stats

```text
GET /api/campaigns/{campaign_id}/stats
```

Response:

```json
{
  "campaign_id": "cmp_123",
  "recipient_count": 120000,
  "sent_count": 119500,
  "skipped_count": 500,
  "delivered_count": 118900,
  "unique_open_count": 42000,
  "open_count": 61000,
  "open_rate": 0.3515,
  "unique_click_count": 8500,
  "click_count": 12200,
  "click_rate": 0.0711,
  "click_to_open_rate": 0.2024,
  "unsubscribe_count": 180,
  "bounce_count": 400,
  "complaint_count": 8
}
```

## Transactional Email APIs

### Send Transactional Email

```text
POST /api/transactional/send
```

Request:

```json
{
  "email": "person@example.com",
  "template_key": "email-verification",
  "idempotency_key": "verify-user-123-email-1",
  "context": {
    "name": "Person",
    "verification_url": "https://client.example/verify/placeholder"
  },
  "metadata": {
    "source": "email-verification"
  }
}
```

Use cases:

- Registration confirmation.
- Password reset.
- Email verification.
- Course enrollment notification.
- Payment or account notices.

Transactional sends should be logged and trackable. They should not require marketing subscription, but they must respect hard suppression states. If the selected template declares required context variables, missing values return a JSON validation error before Datamailer creates contacts, messages, events, or queue payloads.

Template keys are client-scoped and visible to staff in `/templates/`. Staff users can inspect each transactional template's required context and placeholder example context there.

Registration/welcome example:

```bash
curl -sS -X POST "$DATAMAILER_URL/api/transactional/send" \
  -H "Authorization: Bearer <client-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "person@example.com",
    "template_key": "registration-welcome",
    "idempotency_key": "registration-user-123",
    "context": {"name": "Person", "course_name": "ML Zoomcamp"},
    "metadata": {"source": "registration"}
  }'
```

Password reset example:

```bash
curl -sS -X POST "$DATAMAILER_URL/api/transactional/send" \
  -H "Authorization: Bearer <client-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "person@example.com",
    "template_key": "password-reset",
    "idempotency_key": "password-reset-user-123-request-456",
    "context": {"reset_url": "https://client.example/reset/placeholder"},
    "metadata": {"source": "password-reset"}
  }'
```

Email verification lifecycle:

1. The client app creates its own verification URL and sends it through `/api/transactional/send`.
2. The user completes verification in the client app, not in Datamailer.
3. The client app marks the Datamailer contact verified with `PATCH /api/contacts/{contact_id}/verification` and the audience/client scope.
4. Datamailer marketing eligibility uses the verified, subscription, validation, and suppression state.

```python
import requests

headers = {"Authorization": "Bearer <client-api-key>"}

requests.post(
    f"{datamailer_url}/api/transactional/send",
    headers=headers,
    json={
        "email": "person@example.com",
        "template_key": "email-verification",
        "idempotency_key": "verify-user-123-email-1",
        "context": {
            "name": "Person",
            "verification_url": "https://client.example/verify/placeholder",
        },
        "metadata": {"source": "email-verification"},
    },
    timeout=10,
)

requests.patch(
    f"{datamailer_url}/api/contacts/{contact_id}/verification",
    headers=headers,
    json={"audience": "datatalksclub", "client": "dtc-courses", "verified": True},
    timeout=10,
)
```

## Public Tracking Endpoints

### Open Pixel

```text
GET /t/o/{tracking_token}.gif
```

Behavior:

- Records an `open` event.
- Updates first open and open count.
- Returns a transparent 1x1 GIF.

### Click Redirect

```text
GET /t/c/{tracking_token}?u=<encoded-url>
```

Behavior:

- Validates token.
- Records a `click` event.
- Updates first click and click count.
- Redirects to the destination URL.

### Public Unsubscribe

```text
GET /unsubscribe/{unsubscribe_token}
POST /unsubscribe/{unsubscribe_token}
```

Behavior:

- Shows preferences for the recipient and message context.
- Allows client, audience, or global unsubscribe depending on the message.
- Records an unsubscribe event.

## SES Webhook APIs

```text
POST /webhooks/ses
```

Events to process:

- `Delivery`
- `Bounce`
- `Complaint`
- `Open`
- `Click`

SES message IDs should be correlated back to campaign recipient or transactional message rows.
