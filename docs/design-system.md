# Datamailer Design System

Datamailer is an operational email tool. The interface should be clean, easy to scan, and calm under production pressure.

We use our own lightweight CSS system. Do not add Bootstrap, Tailwind, React, a SPA framework, or a required frontend build pipeline unless a future issue explicitly changes this decision.

## References

Use these references for UI elements and interaction quality:

- Resend: API docs, API keys, developer-facing settings, sparse tables, clean examples.
- Stripe Dashboard: dense operational tables, filters, status badges, detail pages, timelines, forms, destructive actions.
- Linear: restraint, spacing rhythm, quiet typography, compact navigation, low visual noise.

Use Postmark only for product concepts, not visual styling:

- delivery activity
- bounces and complaints
- suppression handling
- message streams
- transactional vs broadcast email operations

## Principles

- Clean and easy first.
- Operational clarity over marketing polish.
- Dense but readable beats decorative and oversized.
- The first screen of a detail page must answer the core operational question.
- Primary state and primary action should be obvious.
- Raw provider payloads, audit metadata, and debugging details belong in secondary sections.
- UI labels and URLs should use user language, not implementation language.
- No backwards compatibility is required before production unless an issue explicitly says otherwise.

Core questions by page:

- Contact: can we email this person, and why or why not?
- Campaign: what was sent, to whom, and what happened?
- Audience: who is included, how are they segmented, and who is inactive?
- Client: which integration is this, which API keys are active, and what are they used for?
- Template: what context is required, and what will be rendered?
- API docs: how do I make the request, and what response should I expect?

## CSS Direction

Keep CSS small and explicit:

- design tokens in one place
- reusable layout primitives
- reusable form/table/badge/button patterns
- page-specific CSS only when a reusable primitive is not enough

The default implementation should work with plain Django templates and static CSS.

Preferred file direction:

- `static/mailing/css/app.css` for the design system and product UI
- no large inline CSS in templates
- no generated CSS checked in unless a future build process is approved

## Layout

- Use a stable app shell with clear navigation.
- Prefer compact top navigation or a restrained sidebar if navigation grows.
- Use active navigation states.
- Use breadcrumbs on detail pages when they improve orientation.
- Keep content widths readable, but allow operational tables to use wider space.
- Avoid nested cards.
- Use sections, tables, split layouts, and timelines instead of card grids.
- On mobile, prioritize core state and actions before large tables.

## Typography

- Use a system font stack.
- Keep letter spacing normal.
- Do not scale font size with viewport width.
- Page titles should be clear but not hero-sized.
- Section headings should be compact.
- Table and metadata text should remain readable at 14-15px.
- Help text should be short and muted.

## Color

Use a restrained neutral base:

- strong text
- muted text
- border
- subtle background
- elevated surface only when needed

Use status colors consistently:

- success: verified, delivered, active, subscribed
- warning: pending, risky, skipped, needs attention
- danger: bounced, complained, failed, unsubscribed, revoked
- neutral: draft, unknown, inactive, none

Do not let pages become dominated by one hue or by decorative gradients.

## Components

### Buttons

- One primary action per page region.
- Secondary actions should be visually quieter.
- Destructive actions use danger styling and confirmation.
- Avoid button rows with many equally loud actions.

### Badges

Use badges for short state labels:

- verified / unverified
- valid / risky / invalid
- subscribed / unsubscribed
- active / revoked
- delivered / bounced / complained / failed

Badges should not wrap awkwardly inside tables.

### Tables

Tables are the default for operational lists.

- Keep columns predictable.
- Use truncation for long email, URL, metadata, and subject values.
- Avoid cramming too many columns into the first view.
- Move secondary data into detail pages or expandable rows.
- Preserve filters and pagination state.
- Empty states should say what is missing and what the next action is.

### Forms

- Every input has a visible label.
- Related fields are grouped.
- Validation errors appear next to the relevant field.
- Long text areas need context or preview when the user is composing something important.
- Mutation-heavy forms should not dominate read-only detail pages.

### Detail Pages

Use a summary-first structure:

1. Identity and state.
2. Sendability or operational eligibility.
3. Key metrics.
4. Membership/configuration.
5. Recent activity.
6. Audit/debug details.

If a page becomes long, split it into clear sections or tabs. Do not place every edit form at the top.

### API Docs

API docs should feel closer to Resend than to generated reference docs:

- examples before exhaustive reference
- copy-pasteable curl examples
- demo API keys from local seed data
- example request bodies
- example success responses
- common error responses
- links from examples to API key management

## URL And IA Rules

- Product UI routes should not use `/operator`.
- Client API routes should not use `/api/v1` before production; use `/api`.
- Contact UI URLs should use email addresses instead of numeric IDs.
- Keep Django admin at `/admin/`.
- Route names, page titles, and navigation labels should use the same vocabulary.

## Accessibility

- Inputs must have labels.
- Links and buttons must be keyboard reachable.
- Focus states must be visible.
- Text must not overlap or overflow at mobile widths.
- Tables need accessible headers.
- Public unsubscribe pages must be simple, accessible, and trustworthy.

## Applying This Guide

All UI issues should reference this guide. When changing a page, check:

- Does the first screen answer the page's core question?
- Are actions ranked by importance?
- Are tables readable and not overloaded?
- Are statuses consistent with the shared badge language?
- Is the URL human-readable?
- Would the page still be usable during an incident or production email send?
