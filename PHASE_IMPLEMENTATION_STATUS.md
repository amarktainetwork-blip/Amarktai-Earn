# AmarktAI Earn Implementation Status

## Current phase

### Phase 1 — Owner dashboard and public-site finalisation

Status: IN PROGRESS

Goal: finish the single-owner control surface and public explanation before enabling new revenue connectors.

#### Done
- Revenue-capable account plan reduced to practical South African / non-Stripe routes.
- TaskBounty external public-address payout path corrected; private-key custody remains outside Webdock.
- Nevermined removed from active earning routes until stablecoin execution is implemented and proven.
- Webdock host compliance boundary enforced.

#### In progress
- Make AmarktAI branding and page language uniform across owner dashboard, Treasury, Markets and login.
- Remove ambiguous dashboard labels that make Treasury / earnings responsibilities unclear.
- Rewrite landing hero so it clearly explains that AmarktAI Earn finds, qualifies, executes, checks and tracks earning work under owner rules.
- Align public legal / owner-access pages visually and verbally.
- Add regression tests for the final navigation, branding and autonomous-work explanation.

#### Remaining before Phase 2
- CI must pass on the final dashboard/public-site branch.
- Merge to main.
- Deploy to production and prove the rendered dashboard / landing page.

## Phase 2 — Credentials and onboarding control plane

Status: NOT STARTED

- Per-provider/market credential entry with encrypted storage where appropriate.
- Guided account checklist, KYC/payout proof and connection tests.
- Fail-closed readiness gates: account, API, work, payout receipt and settlement proof.

## Phase 3 — Revenue connectors

Status: NOT STARTED

Implement and prove one route at a time, starting with the highest-value prepared channels and direct payment routes.

## Phase 4 — Controlled live earning

Status: NOT STARTED

- Shadow/live proof per route.
- One real monetary proof per channel.
- Profit Brain economics and limits.
- Enable bounded autonomy only after the route is proven.

## Deferred

Public SaaS / multi-tenant sale of AmarktAI Earn is deliberately deferred until the owner system is proven working and earning.
