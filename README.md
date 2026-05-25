# Datamailer

Datamailer is a standalone Django mailing service for shared audiences, campaigns, transactional email, unsubscribe handling, and engagement stats.

## What is included

- UI and admin for audience management
- Shared contacts with audience/client-specific subscriptions
- Campaign sends with one recipient row per intended contact
- Open tracking, click tracking, unsubscribe, bounce, and complaint handling
- Transactional email API for registration, password reset, email verification, and similar client-app flows
- API for client apps to check whether an email is verified/subscribed
- Postgres source of truth, SQS queues, Lambda workers, and SES delivery

## Design Docs

- [Architecture](docs/architecture.md)
- [Data Model](docs/data-model.md)
- [API Design](docs/api.md)
- [Milestones and Tasks](docs/milestones.md)

## Setup

```bash
make setup
```

Seed local demo data:

```bash
uv run python manage.py seed_demo_data
```

Run the web app:

```bash
make run
```

Log in at `/admin/login/` with:

- Username: `admin`
- Password: `admin`

The product UI uses Django staff auth, so unauthenticated users are redirected to `/admin/login/`.
Staff users can open the local API reference at `/api-docs/`; the OpenAPI JSON is available at
`/api-docs/openapi.json`.
Transactional template keys and required context are visible to staff at `/templates/`.

Client applications authenticate to `/api` with Bearer authentication. In the product UI, open
`/clients/`, create or select a client, then generate or rotate that client's API key. The raw key is
shown once after generation and should be stored by the client application. API access is scoped to the
authenticated client's organization and the request's `audience`/`client` values.

`seed_demo_data` is local-only and idempotent; rerun it after migrations to recreate or refresh the test admin,
organizations, audiences, clients, contacts, tags, campaigns, transactional history, engagement events, and
suppressed contacts without enqueueing SQS work or calling SES.

## Direction

Datamailer should replace expensive Mailchimp-like sending for DataTalksClub and AI Shipping Labs while remaining reusable by multiple client apps.

The first production architecture is intentionally boring:

- Django for UI, API, tracking endpoints, and admin workflows
- Postgres for contacts, subscriptions, campaigns, recipient snapshots, and event history
- SQS for durable send/event queues
- Lambda workers for bursty campaign sending, transactional sending, and webhook/event processing
- Amazon SES for delivery, bounces, complaints, opens/clicks where provider events are available
