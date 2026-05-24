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

Run the web app:

```bash
make run
```

## Direction

Datamailer should replace expensive Mailchimp-like sending for DataTalksClub and AI Shipping Labs while remaining reusable by multiple client apps.

The first production architecture is intentionally boring:

- Django for UI, API, tracking endpoints, and admin workflows
- Postgres for contacts, subscriptions, campaigns, recipient snapshots, and event history
- SQS for durable send/event queues
- Lambda workers for bursty campaign sending, transactional sending, and webhook/event processing
- Amazon SES for delivery, bounces, complaints, opens/clicks where provider events are available
