# Browser walkthrough, all personas (2026-11-12)

Platform: local Docker, http://localhost:8080, XODUS Azure SQL database, active migration MIG-00A1A6577B.
Rules: browser only, no backend changes, no passwords written here. Fresh local test accounts walk3.analyst, walk3.validator, walk3.imo, walk3.sme, walk3.compliance (@example.com) were registered and approved on 2026-11-12 with one role each for this walk, then disabled and deleted from Users & access the same day (the shared test password was never stored in the repository). Mark each line Pass / Fail / Note.

## Status by persona
| Persona | Walk account | Status |
|---|---|---|
| Platform Admin | local demo account | Walked (all setup pages render) |
| Migration Analyst | walk3.analyst@example.com | Walked on stage 08 (see results) |
| Validation Specialist | walk3.validator@example.com | Walked on stage 08 (see results) |
| IMO Lead | walk3.imo@example.com | Partly walked (stage 05 and 08 fixes verified) |
| Domain SME | walk3.sme@example.com | Walked on stage 08 (see results) |
| Compliance Reviewer | walk3.compliance@example.com | Walked on stage 08 (see results) |

## Platform Admin
- [x] Sign in; lands on suite, opening a migration page redirects to setup with the intended role block message.
- [x] Azure connection: Data Factory Saved, Source storage Reachable, Target database Connected.
- [x] Target system (confirmed, 3 entities), Target adapter (v2: 22 approved, 0 waiting, 5 left to migrations), Target code values (registration_type 2 of 14 set; IRA_TRADITIONAL and IRA_ROLLOVER set), Platform knowledge (assets listed), Canonical model 2.0.0, Governance (6/12 gates, 6/6 checks), Tenant settings (EXAMPLE DATA chip), Users & access: all render, no errors.
- [x] Migration list shows 5 migrations, active one MIG-00A1A6577B.

## Checks for the migration personas (analyst, validator, IMO Lead)
1. Overview: rail and "My queue" agree on the current stage and the next action.
2. Stage 05 Mapping validation: lineage table shows "APPROVED (after proposal)" for approved Advisor fields (fix O4); run is marked out of date if a mapping changed after it.
3. Stage 07: package state; analyst sees "Generate the pipeline package" while the package is stale.
4. Stage 08 Publish artifacts: "Published" tile shows "Older package" while the package is stale or a different one was published (fix O3); merge offered only for the current package.
5. Stage 10: "Promote batch to live" disabled for an already promoted batch.
6. Role gating: validator sees no promote or merge; IMO Lead sees analyst and validator actions.

## Checks for Domain SME and Compliance Reviewer
- Domain SME: canonical-model actions enabled (release, change requests, platform knowledge Add); migration actions disabled with a tooltip.
- Compliance Reviewer: no enabled action on any page.

## Findings log (fill during the walk)
| # | Persona | Page | Finding | Severity |
|---|---|---|---|---|
| | | | | |

## Results so far (IMO Lead, walk3.imo)
- Pass: sign-in; Data Migration card says "Generate the pipeline package"; rail agrees (6 of 10 complete, next stage 07).
- Pass (O3): stage 08 "Published" tile reads "Older package"; ADF package v6.
- Pass (O4): stage 05 lineage table shows "APPROVED (after proposal)" for approved Advisor fields; RepCodes, IsActive, Email checked; SMSPhone and Designations show UNMAPPED (real gaps).
- Note: pull request #4 still shows OPEN in stage 08 (closed on GitHub per user; the platform learns of it on Refresh, and has no close action).
- Note: direct links to migration pages redirect to the suite home until the Data Migration space is entered.
- Note (Platform Admin): `/users` is not a route; users are at `/admin/users` (Governance, Users & access).

## Results, other personas (stage 05 / 08 checks, 2026-11-12)
- Validation Specialist (walk3.validator): Pass. Stage 05 renders (intro lists all five entities); "Run validation again" offered; stale-package banner shows ("approved mappings changed, 6 added"). Stage 08: PR #4 shows Refresh only, no approve or merge.
- Migration Analyst (walk3.analyst): Pass (fix 4). PR #4 text: "out of date (2 changes since it was built); generate a new one (stage 07)"; "Run setup pipeline" disabled, "Merge the pull request first".
- Domain SME (walk3.sme): Pass for gating. Every stage 08 action disabled with the tooltip "Your persona may not do this". Note: the Canonical model page shows no action buttons at all for this persona, so release and change-request rights were not exercised; check where those controls live.
- Compliance Reviewer (walk3.compliance): Pass. Every stage 08 control disabled with the same tooltip. Note: this persona and the SME get a "Continue as ..." step before entering the workspace; the analyst and IMO Lead enter directly.
- Migration Analyst, stage 10 Target validation: Pass. Renders; last run TVAL-A7786F44C8 PASS (42 passed, 0 failed).
- Migration Analyst, stage 07 Transformation SQL: Pass. Package ART-CC410727 v6 is marked STALE ("no longer reflects the current approved state"); "Generate and save ADF package" is enabled. NOT clicked: regenerating is held until after the Azure move (O2).
- Not walked: promote batch (stage 10 live promotion), Platform Admin pages beyond Azure connection, and the remaining pages for each persona. 

## O8 business decisions (IMO Lead, checked in the app, nothing changed)
- Code sets: IRA maps to IRA_TRADITIONAL, status APPROVED (decided earlier by Walk Analyst, 2026-11-11). No pending code-set rows.
- Entity resolution: Client and Advisor match rules APPROVED; account-role derivation was already approved (validation passes AccountRole as derived).
- Not decisions in the app: the four per-migration target fields and the placeholder Simplicity code values are open for the business owner; no action possible here.
- Promote step lives on Target validation (/validation-rules), not /execution.

## Platform Admin walk (2026-11-12)
- Pass: all platform setup pages render with no errors; Governance gate counts and Tenant settings still carry the EXAMPLE DATA chip (O7, O6 unchanged).
- Still to check: promote batch control (fix 3), O5 legacy schema, remaining pages of each migration persona.

## Promote batch (IMO Lead, 2026-11-12)
- Pass (fix 3): on /validation-rules, "Promote batch B20261111T081428Z to live" is disabled with "This batch is already promoted; a new batch is promoted after its own sign-off."
- Resolved (not an app bug): the sign-in "glitch" was the browser autofilling the saved demo credentials over the typed ones, and navigating away before the sign-in finished (which shows "Your session ended"). A failed sign-in always redirects to /login?error=...; the lockout (5 failures, 15 minutes) is stored in the database and shows "Too many attempts". Restarting the container was not needed. When walking by browser, wait for each sign-in to land before navigating.

## Domain SME rights (walk3.sme, follow-up)
- Pass: Canonical Change Requests page (/canonical-changes) renders for the SME with the owner text ("you approve, reject or defer below") and the Canonical releases panel (this workspace is on release 1).
- Not exercised: approve, reject or defer, because the migration has no canonical change requests yet. To exercise them, raise one as the Migration Analyst (Target mapping gap, or a change request from the mapping page) and decide it as the SME.
- The Canonical model page itself shows no action buttons for the SME unless a release draft exists.

## Delete account and test-account cleanup (2026-11-12)
- New: Users & access has a "Delete account" button on pending and disabled accounts (an active account is disabled first; nobody deletes their own account). Deletes are written to the access history with the roles the account held.
- Browser check as Platform Admin: disabling walk3.analyst showed the Delete button on that row only (active rows offer Disable but no Delete); deleting it showed "walk3.analyst@example.com deleted." and the access history logged "Account disabled" then "Account deleted". Repeated for walk3.validator, walk3.imo, walk3.sme and walk3.compliance. Remaining users: the demo admin, mock.tester and the older walk.* accounts (untouched).
