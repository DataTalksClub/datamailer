# Datamailer Design System

Datamailer is an operational tool. The UI should be quiet, dense, and clear. Avoid marketing-page composition unless explicitly requested.

## UI Direction

- Use Django templates/admin first.
- Keep pages fast and lightweight.
- Prefer tables, filters, forms, and detail pages over decorative cards.
- Use HTMX only when it materially improves operator workflow.
- Do not introduce a SPA or heavy frontend build pipeline by default.

## Layout

- Standard page width: `max-width: 1120px`.
- Page padding: `20px` on mobile, `32px` vertical on desktop.
- Use clear page headings and compact sections.
- Avoid nested cards.
- Use tables/list rows for contacts, campaigns, recipients, and events.

## Typography

- System font stack is acceptable.
- Page h1: 26-32px.
- Section headings: 18-22px.
- Table/list text: 14-15px.
- Metadata/help text: 12-13px.
- Avoid oversized hero text inside operational screens.

## Color

Use a restrained neutral base:

- primary text
- muted text
- border
- subtle surface
- clear status colors for sent/failed/bounced/unsubscribed

Do not let the UI become a one-hue palette. Status colors should communicate state, not decoration.

## Interaction

- Buttons should have clear labels or familiar icons with tooltips.
- Forms need explicit labels and validation errors.
- Destructive actions require confirmation.
- Filters should preserve query state.
- Tables should have predictable columns and empty states.
- Public unsubscribe/preference pages must be simple, accessible, and trustworthy.

## Accessibility

- Inputs must have labels.
- Links/buttons must be keyboard reachable.
- Focus states must be visible.
- Text must not overlap or overflow at mobile widths.
- Public pages must work without login unless explicitly protected.
