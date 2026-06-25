# datamailer

Command-line client for sending email through a [Datamailer](https://github.com/DataTalksClub/datamailer)
deployment. Built for the "an agent/script finished — just email me the result" case:
no need to push to GitHub or open a dashboard, just pipe the output into an email.

It is a thin, dependency-free wrapper over Datamailer's transactional API. It works
against any Datamailer instance — yours or someone else's — configured with a URL and a
client API key.

## Install

```bash
pip install datamailer
# or, run without installing:
uvx datamailer --help
```

Requires Python 3.11+.

## Configure

A Datamailer operator issues you a client API key (`dm_...`) and tells you the URL where
their instance is deployed. Then:

```bash
datamailer configure --url https://datamailer.example.com --api-key dm_xxx
# optionally set a default recipient and sender:
datamailer configure --default-to me@example.com --default-from results
```

Settings are stored in `~/.config/datamailer/config.toml` (mode `0600`). You can override
or skip the file entirely with the environment variables `DATAMAILER_URL` and
`DATAMAILER_API_KEY`, or per-command with `--url` / `--api-key`.

Verify the connection:

```bash
datamailer whoami
```

## Send

```bash
# inline body
datamailer send --to me@example.com --subject "Pipeline done" --body "All 42 jobs passed."

# body from stdin — ideal for agents and scripts
./run_pipeline.sh | datamailer send --to me@example.com --subject "Pipeline output"

# body from a file, as HTML
datamailer send --to me@example.com -s "Report" --body-file report.html --html
```

The first send against a deployment auto-creates a generic transactional template
(`cli-message`, or `cli-message-html` for `--html`) on your client, then reuses it. Your
subject and body are passed as the template context.

Other commands:

```bash
datamailer status <message_id>     # delivery status + event timeline
datamailer senders                 # list configured sender addresses
datamailer senders --set 'results=Agent <agent@example.com>' --default results
```

Add `--json` to `send`, `status`, `whoami`, or `senders` for machine-readable output.

## Notes

- Sends require the client to have a verified sender address configured. If `whoami`
  shows no sender, set one with `datamailer senders --set ...` (the address must be
  verified in the deployment's SES).
- Transactional sends are blocked for recipients that hard-bounced or complained.
