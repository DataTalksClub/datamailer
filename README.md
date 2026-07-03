# Datamailer

Datamailer is a standalone Django mailing service for shared audiences,
campaigns, transactional email, and unsubscribe handling. It also tracks
engagement stats.

## Included Features

Datamailer includes:

- UI and admin for audience management
- Shared contacts with audience/client-specific subscriptions
- Campaign sends with one recipient row per intended contact
- Open tracking, click tracking, unsubscribe, bounce, and complaint handling
- Transactional email API for registration, password reset, email verification, and similar client-app flows
- Optional per-client one-way Mailchimp sync that tags contacts in a client's Mailchimp audience when they join a mapped recipient-list node
- API for client apps to check whether an email is verified/subscribed
- Postgres source of truth, SQS queues, Lambda workers, and SES delivery

Infrastructure Terraform is kept in the private `DataTalksClub/datamailer-infra` repository. This public
repo keeps app code, tests, CloudFormation skeletons, and smoke scripts.

## Design Docs

Read these docs for the main architecture and API details:

- [Architecture](docs/architecture.md)
- [Data Model](docs/data-model.md)
- [API Design](docs/api.md)
- [Milestones and Tasks](docs/milestones.md)

## CLI client

[`cli/`](cli/) is a standalone, dependency-free command-line client published to PyPI as
[`datamailer`](https://pypi.org/project/datamailer/).

Use it to send email through any Datamailer deployment with a URL and a client
API key:

```bash
pip install datamailer
datamailer configure --url https://datamailer.example.com --api-key dm_xxx
./run_pipeline.sh | datamailer send --to me@example.com --subject "Pipeline output"
```

See [cli/README.md](cli/README.md) for usage details. The CLI is versioned and
released independently from this backend as `datamailer-backend`.

## Setup

Install dependencies and run migrations:

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
Staff users can open the local API reference at `/api-docs/`. The OpenAPI JSON is
available at `/api-docs/openapi.json`.
Transactional template keys and required context are visible to staff at `/templates/`.

## Local capture mode

Run Datamailer locally with Postgres and capture delivery:

```bash
make capture-up
```

The command starts the app on `http://localhost:8001`, runs migrations, seeds
demo data, and enables `DATAMAILER_DELIVERY_MODE=capture`. Log in with
`admin` / `admin`.

The seeded `dtc-courses` client has this local API key:

```text
dm_dtccourses_demo_transactional_email_key
```

You can view captured transactional and campaign renders through the product UI
and the testbed API at `/api/testbed/runs`.

Client applications authenticate to `/api` with Bearer authentication. In the product UI, open
`/clients/`, create or select a client, then create a named API key for each integration. The raw key is
shown once after generation and should be stored by the client application. API access is scoped to the
authenticated client's organization and the request's `audience`/`client` values.

After migrations, rerun the local-only `seed_demo_data` command to refresh the
test admin and core API records. It also refreshes demo contacts and campaigns.
Transactional history, engagement events, and suppressed contacts are refreshed
without enqueueing SQS work or calling SES.

## Direction

Datamailer should replace expensive Mailchimp-like sending for DataTalksClub and AI Shipping Labs while remaining reusable by multiple client apps.

The first production architecture is intentionally boring:

- Django for UI, API, tracking endpoints, and admin workflows
- Postgres for contacts, subscriptions, campaigns, recipient snapshots, and event history
- SQS for durable send/event queues
- Lambda workers for bursty campaign sending, transactional sending, and webhook/event processing
- Amazon SES for delivery, bounces, complaints, opens/clicks where provider events are available
