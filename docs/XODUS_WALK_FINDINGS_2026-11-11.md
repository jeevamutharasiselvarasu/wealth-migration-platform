# Browser walk, all roles: findings (2026-11-11)

Local platform (Docker) on the XODUS Azure SQL database (schema `xodus_app`), migration MIG-00A1A6577B. Walked as all six personas with
local walk accounts: Platform Admin, Migration Analyst, Validation Specialist, IMO Lead, Domain SME, Compliance Reviewer (the last two
were registered and approved for this walk). Nothing was sent outside the platform: no pull request, merge, Data Factory run or
promotion was started. Actions taken during the walk (internal, read-only or expected next steps): connection tests, target-mapping
and entity-resolution previews, adapter version 2 adopted (no change), mapping validation re-run, live validation re-run (PASS).

## How it was checked

1. Automated crawl: every page (39) and every internal link as each persona. Result: no server error, no broken link, no template
   leftover. The only redirects are the intended role blocks (migration personas cannot open Users, Azure, GitHub, Target system,
   Target adapter, Target code values).
2. Browser pass per persona: page layout, rail and dashboard state, which actions are offered, and read-only actions run.

## Fixed during the walk

| # | Where | Finding | Fix |
|---|---|---|---|
| 1 | Stage 05, rail | Mapping validation stayed "complete" after new mappings (Advisor) were approved; the run never covered them. | A run is out of date once a mapping is approved or changed after it: the rail drops to stage 05, the page says so, the validator's queue says "Run validation again". |
| 2 | Stage 05 | Re-run failed AccountRole "No approved mapping for ACCT_NUM, CLIENT_ID, ROLE" although AccountRole is derived (approved). Out-of-scope entities were also checked. | Validation knows derived entities (PASS, naming the derivation) and checks only the migration's scope. Re-run: PASS, 15 checks. |
| 3 | Stage 10 | "Promote batch … to live" stayed enabled for a batch already promoted (analyst, IMO Lead). | Button disabled with "already promoted"; the server also refuses a second promotion of the same batch (or one still running). |
| 4 | Stage 08 | Pull request #4 (outdated package) could still be approved and merged. | Merge offered only for the current, up-to-date package; otherwise "superseded" or "out of date, regenerate first". The server refuses it too. |
| 5 | All pages | Buttons were shown to personas the server refuses (Domain SME: preview/save rules, sign-off evidence, refresh). | Every page disables forms the persona may not post (same policy the server enforces), with a tooltip. Compliance Reviewer: no enabled action on any page; IMO Lead keeps all. |
| 6 | Overview | "My queue" said "Nothing needs you right now" while a stage action was waiting (code values, match rules, stages 07-10). | Queue lists those steps for the role that owns them (analyst now sees "Generate the pipeline package"). |
| 7 | Overview | "Pipeline by plane" dots were all grey. | Dots show each stage's state, as the rail does. |
| 8 | /models (admin) | "Target system: Unavailable" (it still called the old simulated service). | Shows the confirmed Azure SQL target (3 tables). |
| 9 | Setup tab bar | Canonical model, Target adapter, Target code values missing. | Added. |
| 10 | Discovery | "Entities inferred" listed Account, Client, Household (hard-coded). | Lists the entities actually inferred. |
| 11 | Stage 05 | Intro said "for Account, Household and Client". | Lists the migration's scope. |
| 12 | Stage 09 | Texts said "manual run in ADF"; "Deployment package: Ready" while the package is stale; obsolete "Rewrite canonical files to stage/" button. | Texts describe the stable pipelines; tile says "Out of date"; obsolete button removed for the stable-pipeline package. |
| 13 | Canonical model page | Note said Advisor/AccountRole discovery and mapping "follow in a later phase". | Note describes what is built. |
| 14 | Migration setup (admin) | Read-only notice said "(or Platform Admin)". | Wording corrected (the fields were already locked). |
| 15 | Target adapter | Notice said "rejected" for "Leave to migration"; "Confirm version" offered with nothing changed. | Wording matches the button; confirm offered only when the draft changed. (Fixed earlier today.) |

## Open (not fixed; for a decision or later)

| # | Where | Finding | Suggestion |
|---|---|---|---|
| O1 | Blob storage | The platform gets HTTP 403 (authorization failure) from the storage account. | Storage firewall: allow the current IP (and later the Container App's addresses). |
| O2 | Stage 07/08 | The package is stale (Advisor, match rules, account-role derivation): regenerate, then open a new pull request. Held until after the Azure move, as decided. | Regenerate after hosting; close pull request #4 on GitHub (the platform has no "close" action). |
| O3 | Stage 08 | "Published: Yes" tile refers to the last published package even when stage 07 is stale. | **Fixed:** tile shows "Older package" when the published package is not the current one or is stale. |
| O4 | Stage 05 | The field-lineage table shows proposal-time statuses (Advisor fields "PROPOSED_NEEDS_REVIEW") though they are approved. | **Fixed:** shows "APPROVED (after proposal)" for fields now approved; page text says status is the state now. |
| O5 | XODUS database | A legacy `simplicity` schema (ORION tables, xodus_run_log) remains from before the separate Simplicity database. | Drop it after confirming nothing reads it. |
| O6 | Tenant settings | Brand settings are kept in the browser only (stated on the page). | Store them on the platform if they must be shared. |
| O7 | Governance | Gate counts are example data (chip "EXAMPLE DATA"). | Connect to the real gates when Governance is in scope. |
| O8 | Business decisions | "IRA" mapped to IRA_TRADITIONAL; account roles derived from the household head; four target fields left to each migration (OPEN_CLOSED, PHONE_HOME, HOUSEHOLD_DOB, ORION_HOUSEHOLD_ID); placeholder Simplicity code values. | Confirm with the business owner. |
| O9 | Browser | Two screenshot time-outs on Transformation SQL / Overview; the page is 44 KB and loads in about 1 s, not reproducible. | None (tooling). |

## Per persona

- **Platform Admin**: platform setup pages, Canonical model (release 2.0.0), Target adapter (v2), Target code values, Platform knowledge, Users,
  Governance, Tenant settings all render. Connection tests: Data Factory, XODUS database, Simplicity database, GitHub sign in; blob 403 (O1).
  Migration list and setup are read-only; migration data pages are blocked. The adapter page shows migration ids (configuration, by design).
- **Migration Analyst**: stages 01-10 render; previews work (target mapping, entity resolution); adapter v2 adopted (0 changes); rail and
  queue agree (stage 07, generate the package).
- **Validation Specialist**: mapping validation run (PASS after fixes 1-2); live validation PASS; promote and merge not offered.
- **IMO Lead**: both analyst and validator actions available on every page (decided earlier).
- **Domain SME**: canonical-model rights (release, change requests, platform knowledge "Add"); migration actions disabled (fix 5).
- **Compliance Reviewer**: read-only everywhere; no enabled action on any page (fix 5).
