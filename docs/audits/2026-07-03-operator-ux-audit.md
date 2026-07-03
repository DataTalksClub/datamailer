# Operator UI usability audit — 2026-07-03

Tracking epic: [#67](https://github.com/DataTalksClub/datamailer/issues/67). Prompted by product-owner feedback that the operator UI "is very difficult to use, it feels very raw and very technical."

Method: heuristic evaluation against `docs/design-system.md` plus task-based walkthroughs of the core operator jobs-to-be-done, logged in as staff with `seed_demo_data`, desktop (1440px) and mobile (390px). Screenshots captured under `.tmp/` (gitignored) and via the sandbox uploader.

## Summary

The UI is structurally sound — it follows the design-system shell and shared primitives, and the contact detail page is genuinely good (summary-first, "can we email this person and why" answered up top, diagnostics tucked into `<details>`). The "raw and technical" feeling comes from four repeating patterns, not a broken layout:

1. **Infrastructure internals leak onto operator screens.** The dashboard's most prominent panel is a Worker Status table full of `systemd` unit names, `process_sqs_worker` commands, and red "Missing" badges. Raw provider payloads (`{"feedback_type": "abuse"}`, `bounce_type: Permanent`, `reason: ses_rejected`, SES message IDs) appear in primary content across dashboard, campaign detail, and audience detail.
2. **Engineering vocabulary in labels and actions.** The primary send action is "Snapshot and queue"; template `key` slugs render as `<code>` everywhere; `normalized_email` is surfaced as helptext; enum names ("No MX", "SNAPSHOTTING") show through.
3. **Density without ranking.** Campaign list has 10 columns (overflowing at 1440px); campaign detail has 14 stat tiles (all zero on a draft); audience detail dumps a 10-field filter grid + tag checkboxes by default. Percentages appear wrong ("Delivered 4 / 133.3%").
4. **Dead-end numbers and missing "what next" affordances.** Almost every count is read-only; deliverability items don't name the affected contact.

Two workflow gaps surfaced: "send a transactional email" has no UI at all, and "see what's queued" requires a hidden URL + the right active client.

## Findings (severity-ranked)

| # | Severity | Surface | Problem | Child issue |
|---|---|---|---|---|
| 1 | HIGH | Dashboard | Worker Status panel exposes systemd units, commands, PIDs, red "Missing" badges | [#68](https://github.com/DataTalksClub/datamailer/issues/68) |
| 2 | HIGH | Dashboard / campaign / audience | Raw provider payloads & SES IDs in primary content | [#69](https://github.com/DataTalksClub/datamailer/issues/69) |
| 3 | HIGH | Dashboard | Deliverability Attention rows don't identify the contact | [#70](https://github.com/DataTalksClub/datamailer/issues/70) |
| 4 | HIGH | Campaign detail | Primary send action "Snapshot and queue", no confirmation | [#71](https://github.com/DataTalksClub/datamailer/issues/71) |
| 5 | HIGH | Campaign list | 10 columns overflow; names wrap char-by-char; engineer-facing speed/queue columns | [#72](https://github.com/DataTalksClub/datamailer/issues/72) |
| 6 | HIGH | Campaign detail | 14 stat tiles; confusing rates; all-zero on drafts | [#73](https://github.com/DataTalksClub/datamailer/issues/73) |
| 7 | HIGH | Transactional queue | Unreachable from nav; client-scoped-empty by default (extends #65) | [#74](https://github.com/DataTalksClub/datamailer/issues/74) |
| 8 | MEDIUM | Audience detail | Full filter grid + tag checkboxes open by default; membership table overflow/overlap | [#75](https://github.com/DataTalksClub/datamailer/issues/75) |
| 9 | MEDIUM | Audience detail | Long lists of zero-count internal enums; raw validation labels | [#76](https://github.com/DataTalksClub/datamailer/issues/76) |
| 10 | MEDIUM | Contact detail | "Manage" is a bottom-of-page disclosure split into 4 mini-forms | [#77](https://github.com/DataTalksClub/datamailer/issues/77) |
| 11 | MEDIUM | Cross-cutting | Terminology & identifier leakage (template key, normalized_email, "Locked", "SNAPSHOTTING") | [#78](https://github.com/DataTalksClub/datamailer/issues/78) |
| 12 | LOW/MED | Cross-cutting | Mobile: sidebar stacks above content; tables overflow | [#79](https://github.com/DataTalksClub/datamailer/issues/79) |
| 13 | MEDIUM | Cross-cutting | Dead-end counts everywhere (generalizes #66) | tracked via #66 + #70 |

## Sequencing

**Quick wins (small template/label/link changes):** #71 (rename send + confirm), #68 (demote worker status), #70 (contact-first attention rows), #69 (humanize metadata), #72 (slim campaign list), #73 (hide draft stats / fix rates), #78 (terminology), #74 (queue nav + empty state).

**Larger redesigns:** #73 (campaign stats grouping), #75 (audience filter pattern + overflow), clickable-counts framework spanning #66/#70, #77 (contact Manage consolidation), #79 (mobile pass).

## Relation to #65 and #66

- **#65** (transactional queue drill-down) shipped. The audit adds two gaps not in its scope — no sidebar nav to the queue, and a confusing client-scoped empty state — tracked in [#74](https://github.com/DataTalksClub/datamailer/issues/74).
- **#66** (clickable audience summary counts) remains the audience-scoped first pass of the broader "dead-end numbers" theme; the dashboard-level twin is [#70](https://github.com/DataTalksClub/datamailer/issues/70).
