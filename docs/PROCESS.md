# Development Process

## Overview

Datamailer uses GitHub Issues to track all product and engineering work. The workflow is role-based and issue-driven: the orchestrator coordinates, role agents do the work, and every shipped issue goes through implementation, technical verification, product acceptance, commit, push, and on-call CI review.

This process was copied from AI Shipping Labs and adjusted for Datamailer.

## Links

- Repo: https://github.com/DataTalksClub/datamailer
- Issues: https://github.com/DataTalksClub/datamailer/issues
- Docs: [`docs/`](./) folder in this repo

## Issue Lifecycle

```text
Orchestrator files/selects issue
  -> Product Manager grooms
  -> Software Engineer implements
  -> Tester verifies
  -> Product Manager accepts
  -> Software Engineer commits locally
  -> Orchestrator pushes main
  -> On-Call Engineer verifies CI/CD
```

1. Orchestrator captures raw user requests as GitHub issues and keeps the pipeline moving. The orchestrator does not implement, test, or accept issue work.
2. Product Manager grooms raw issues into agent-ready specs: scope, acceptance criteria, dependencies, test notes, and labels.
3. Software Engineer implements one groomed issue at a time and writes tests. The engineer does not commit until tester and PM acceptance pass.
4. Tester reviews the uncommitted work, runs the relevant test suite, verifies acceptance criteria, and posts a pass/fail report.
5. Product Manager does final acceptance from the operator/user perspective and posts accept/reject.
6. Software Engineer commits locally with `Closes #N` after approval.
7. Orchestrator pushes `main`.
8. On-Call Engineer checks CI/CD. If CI fails, on-call reopens/comments on the issue, fixes or routes the fix, and verifies green CI.

## Agents

| Agent | File | Role |
|---|---|---|
| Product Manager | `.claude/agents/product-manager.md` | Grooms issues and performs final acceptance review |
| Designer | `.claude/agents/designer.md` | Audits UI surfaces against `docs/design-system.md`; does not implement |
| Software Engineer | `.claude/agents/software-engineer.md` | Implements code and tests; does not commit until approved |
| Tester | `.claude/agents/tester.md` | Runs tests and verifies acceptance criteria |
| On-Call Engineer | `.claude/agents/oncall-engineer.md` | Monitors CI/CD after push and fixes/routes failures |

## Orchestrator Responsibilities

- Stay in the orchestrator role. Do not personally groom, implement, test, or accept issue work when a role agent can own it.
- Keep role boundaries strict. Do not ask one role agent to approve its own work, substitute tester review for PM acceptance, or substitute PM acceptance for test verification.
- File raw intake issues immediately when the user provides new requests, bugs, screenshots, links, or decisions that are not tracked yet.
- Launch role agents asynchronously whenever possible.
- Keep at least one eligible issue moving while backlog exists.
- Groom `needs grooming` issues first.
- Pick the lowest-numbered unblocked groomed issues first.
- Run independent issues in parallel only when their write sets and dependencies do not collide.
- Before launching implementation work, make sure the main checkout is clean enough for the workflow. If there are local changes, either commit approved work, assign those changes to the current SWE agent, or ask the user before stashing/discarding.
- When a SWE reports done, launch tester.
- If tester fails, relay feedback to SWE and repeat until tester passes.
- If tester passes, launch Product Manager acceptance.
- If PM rejects, relay feedback to SWE and repeat until PM accepts.
- After PM accepts and SWE commits locally, push `main`.
- After every push, launch On-Call Engineer to check CI/CD. Do not skip this or replace it with a casual manual glance.
- When CI fails, route the failure to On-Call Engineer first. On-call may fix directly or ask the orchestrator to dispatch SWE if the fix is substantive.
- Commit each completed issue/task immediately after tester and PM approval. Do not accumulate multiple finished tasks in one commit.
- When running multiple agents in parallel, use isolated worktrees or clearly disjoint write scopes. Do not let two agents edit the same files at the same time.

## Mandatory Steps

Every issue must go through:

```text
PM groom -> SWE implement -> Tester review -> PM acceptance -> Commit -> Push -> On-call CI check
```

Never skip:

- Product Manager must groom before implementation unless the issue is already explicitly groomed.
- Tester must run tests, not just inspect code.
- Tester must report exact commands and pass/fail results.
- Product Manager must perform final acceptance after tester PASS and before commit.
- Tester and/or SWE must update acceptance checkboxes in the issue body.
- Role agents must post issue comments via `gh`.
- Each completed issue/task must be committed separately after approval.
- Commits must reference the issue with `Closes #N` or `Refs #N`.
- On-call must check CI after push.
- No GitHub PRs are used unless the user explicitly changes the process.

## Commit And Push Policy

Datamailer uses direct commits to `main` after the role-agent review pipeline passes.

For a single checkout workflow:

1. SWE implements and waits for tester + PM acceptance.
2. SWE or orchestrator commits approved changes locally:
   ```text
   Short imperative subject

   Closes #N
   ```
3. Orchestrator pushes `main`.
4. On-call verifies CI.

For parallel worktrees:

1. SWE works in an isolated worktree/branch and commits locally after approval.
2. Orchestrator merges exactly one completed issue/task into local `main`.
3. Orchestrator pushes `main`.
4. On-call verifies CI before that issue/task is considered shipped.
5. Repeat for the next completed issue/task; do not batch multiple approved issues into one commit.

Do not use `gh pr create` or `gh pr merge` unless the user explicitly changes the process.

## How To Pick Issues

```bash
gh issue list --repo DataTalksClub/datamailer --state open --limit 50 \
  --json number,title,labels \
  --jq 'sort_by(.number) | .[] | "#\(.number) \(.title) [\(.labels | map(.name) | join(", "))]"'
```

Rules:

- Skip `needs grooming`.
- Skip blocked issues.
- Respect `Depends on:` in issue bodies.
- Pick lower-numbered foundational issues first.
- Parallelize only independent work.

Current early dependency shape:

- #2 core data model can proceed after #1.
- #3 API depends on #2.
- #4 worker contracts can proceed after #1 and is useful before #5/#7/#9.
- #5 local AWS test environment can proceed after #1 and should land before real sender work.
- #6 transactional API depends on #2/#3 and benefits from #4/#5.
- #7 transactional sender depends on #4/#5/#6.

## Testing Policy

Default commands:

```bash
make setup
make test
make lint
```

Local AWS integration tests:

```bash
make localstack
make test-aws-local
```

Guidelines:

- Unit and service tests should run without Docker or real AWS.
- AWS-adjacent integration tests should use LocalStack or deterministic boto stubs.
- Real AWS credentials are not required for local tests or CI.
- SES payload correctness should be tested with `botocore.stub.Stubber`.
- SQS/Lambda worker tests should invoke Python handlers directly with SQS-shaped events.
- UI tests should start with Django view/template tests. Add Playwright only for critical user/operator workflows once the UI is substantial.
- Capture screenshots only for UI issues with visible page changes. For backend-only issues, tester should explicitly state screenshots are not applicable.

## Screenshot Upload Workflow

Use the shared sandbox screenshot uploader for throw-away screenshot links. Do not create or update an orphan `screenshots` branch.

For UI-visible issues:

1. Save screenshots under `.tmp/` in this repo.
2. Read `/home/alexey/git/sandbox-screenshots/README.md` before uploading.
3. Install or refresh the CLI if needed:
   ```bash
   cd /home/alexey/git/sandbox-screenshots
   ./install.sh
   source ~/.bashrc
   ```
4. Upload each screenshot:
   ```bash
   upload-screenshot /home/alexey/git/datamailer/.tmp/screenshot.png
   ```
5. Copy the returned `url` value into the GitHub issue comment, QA report, designer audit, or PM acceptance notes.

Do not paste `SCREENSHOT_UPLOAD_TOKEN` into chat, logs, commits, issue comments, or docs. Screenshot objects are temporary and expire automatically.

If a local or remote `screenshots` branch exists, delete that branch, reupload the relevant screenshot files with `upload-screenshot`, and update any issue comments that pointed at the old branch URLs.

## Datamailer Engineering Rules

- Do not send email from HTTP request handlers. Enqueue durable work.
- Postgres is the source of truth.
- SQS is the durable work boundary.
- Lambda workers are at-least-once processors; all worker logic must be idempotent.
- Transactional email must not be blocked behind campaign blasts.
- Campaign sends must snapshot one `campaign_recipients` row per intended recipient.
- Delivery, tracking, unsubscribe, bounce, complaint, and failure transitions must be auditable through `email_events`.
- Unsubscribe and suppression logic require focused tests.
- Keep the frontend thin: Django templates/admin first, HTMX only when useful, no SPA by default.

## Labels

| Category | Labels |
|---|---|
| Workflow | `needs grooming`, `blocked` |
| Priority | `P0`, `P1`, `P2` |
| Area | `architecture`, `infra`, `backend`, `frontend`, `api`, `email`, `tracking`, `subscriptions`, `campaigns`, `transactional`, `ses`, `workers`, `ops`, `docs` |
| Special | `human` |

## Human Verification

Some criteria may be marked `[HUMAN]`, such as SES production access, DNS changes, external webhook delivery, or production pilot sends.

When all automated work is complete but `[HUMAN]` criteria remain:

1. Commit and push the code with `Refs #N` if the issue should stay open.
2. Add the `human` label.
3. Comment with the exact manual checks needed.
4. Continue the pipeline on other issues.

## Temporary Files

Use `.tmp/` inside the project root for temporary files, screenshots, scratch exports, and local previews. Do not write temporary project files outside the repo. Do not commit `.tmp/`.

## Short-Lived Docs

Short-lived audits, plans, and one-off analyses belong in `docs/audits/` with `YYYY-MM-DD-<topic>.md` names. Evergreen references stay at the `docs/` root.

## Technology Stack

- Backend/control plane: Django, managed with `uv`.
- Database: Postgres in production, SQLite allowed locally.
- Delivery: Amazon SES.
- Queues: SQS.
- Workers: Lambda for bursty sender/webhook/event work.
- Frontend: Django templates/admin, minimal CSS, no heavy JS by default.
- Tests: pytest, pytest-django, LocalStack/mocks for AWS-adjacent tests.
