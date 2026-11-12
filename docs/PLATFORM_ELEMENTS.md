# XODUS platform: pages, elements and walk record

Walked 2026-11-06 in workspace **MIG-DF760032C3** ("Platform walk - CoreCap to Orion MCL"), CoreCap to Orion MCL, Azure SQL target, signed in as the Platform Admin. Every approval below was made in the platform with a comment; the reviewer identity comes from the signed-in session.

## Shell and navigation
- **Top bar**: Xodus brand (to /suite); space tabs Deal Room, Data Migration, Governance; persona selector (Switch) that scopes the workspace but grants no extra permission; Theme toggle.
- **Left rail (Data Migration)**: back to Migration workspaces; Overview; Setup; "Next" shortcut; ten stage links, each with a status (Complete / Needs attention / Not started) and the owning persona (MA Migration Analyst, VS Validation Specialist); Command center (template preview).
- **Banners**: notice (green) and "Action needs attention" (red) strips after each POST.

## Workspace level
| Page | Purpose | Elements |
|---|---|---|
| /migrations | Workspaces of the firm | Active card with Open workspace and Setup; per other workspace Open, Setup, Archive (hides, keeps audit log; active cannot be archived), Start a new migration from this (copies configuration only); + New migration |
| /migration-setup | Define a migration | Migration name, target system, canonical version, source firm, target owner, entities (Account, Household, Client), knowledge schema and rules references, SME, relationship notes, source acquisition (sample files or Azure connection), source details; Save setup |
| /setup | Setup hub | Cards linking Workspace configuration, Target system, Firm knowledge, Azure storage & factory, Target database (Azure SQL), GitHub integration |
| /firm-knowledge | Knowledge assets | Built-in wealth kernel; supplementary data assets (Attach Data Asset by category, Import Confluence page); acquisition discovery questionnaire (firm, source systems, data owner, mapping approver, identifier notes); Save Mapping Context |
| /azure-setup | Azure connection | Blob (container, folders: Create folders), Data Factory (subscription, resource group, factory, tenant, client id, secret env var), staging/target resources, Azure SQL (server, database, schema, key vault refs), Test connection, Save, Test SQL connection; CONNECTED badge |
| /github-setup | GitHub integration | Repository, branch, ADF folder, token env var; Save, Test connection |
| /models | Target and canonical models | Supplied target system tables, canonical model tables (read only) |
| /canonical-changes | Change control | Raise a change request (entity, field, type, proposal, reason); list of requests; approve and apply by the Domain SME |
| /admin/users | Users and access | Registered users, approve/disable, role checkboxes (Save roles), access history |
| /governance | Oversight (template preview plus live links) | Governance administration card (Canonical change requests, Users & access with waiting count for platform roles); KPIs; gate chain D1 to C2; integrity checks with Test the skip; policy in force; audit trail with filters; access matrix |
| /deal-room, /tenant, /migration-workspaces | Template preview spaces | Sample data only |

## The ten stages (walk record)
| # | Stage and page | What is on the page | Walk action and result |
|---|---|---|---|
| 01 | Loader & Inspector, /entity-sample-files | Source connection card (storage account, folder, access method, status); KPIs files / rows / size; Blob checklist; Load ticked files; loaded files table with size, rows, hash | Loaded 4 files from raw/corecap/ (146 rows, 33.8 KB); 3 classified, advisors file flagged for review |
| 02 Discovery, /discovery | Source inventory with inferred entity, confidence, evidence, suggested fields; cross-file analysis; relationship candidates (Confirm / Reject with reason); conflicting values; entity-level approval (Approve all X files) with comment; Confirm selected / Reject selected with notes; Profile approved files & generate mappings | 4 files confirmed with per-file notes (advisors classed with Household); 3 relationships confirmed with reasons; Account and Client approved at entity level |
| 03 Business rules, /transformation-rules | Infer rules from asset or text; suggestions from source data; create a rule version (target fields, preview on samples, Save as draft); derived canonical fields (entity, field, input, governed rule, reason; Approve derivation, Remove); rule versions with Retire | Derived Client.LEGAL_NAME (CLIENT_LEGAL_NAME_JOIN v1) and Account.CUSTODIAN_CODE (CUSTODIAN_CODE_MAP v2) |
| 04 Schema Mapper, /agent-pipeline | Pending proposals per entity (source field, inferred canonical, change-to, confidence, transformation, note); Approve/Reject selected; Approve all X mappings; approved and rejected lists with Reopen; information requests with Record response | 37 mappings approved (Account 12, Client 19, Household 6); 5 advisor-lookup proposals rejected with a note; 2 information requests answered with governed rules |
| 05 Mapping Validation Review, /reconciliation | Approved mappings, lineage and run evidence, recent runs, validation results table; Run validation again; Save sign-off evidence | Validation PASS (13 passed, 0 warnings, 0 failed); sign-off evidence v1 saved (ART-6BA0A6CA) |
| 06 Target mapping, /target-mapping | Target setup (locked once confirmed, Reopen setup); gap report with Raise change request; per-entity proposals with canonical, rule and note; Approve / Reject / Reopen selected; Preview (dry run) | 27 proposals: 22 approved, 5 optional unresolved rejected with a note; preview run |
| 07 Transformation SQL, /transformation-sql | Generated T-SQL per entity (approved mappings only), staging tables, warnings; Generate and save ADF package; Write canonical files to stage; deployment package Generate | ADF package v1 (ART-CE807CD8), deployment package v1 (PKG-623D7561) |
| 08 Publish artifacts, /publish-artifacts | Publish ADF package to GitHub; Replace earlier XODUS files and publish (optionally prune generated linked services) | Published 17 files, removed 12 old, commit d11b274 on xodus-amp main |
| 09 ADF Execution, /execution | Deployment package and latest run status; approval to start; manual-run monitor that reads xodus_run_log every 30 s (RUNNING, SUCCEEDED, FAILED, STALLED); step table (rows read and written); diagnosis with recommendations; Rewrite canonical files to stage/ | Canonical files written to stage/migdf760032c/ (60, 40, 46 rows). Run is started by hand in ADF; see the run section below |
| 10 Target validation, /validation-rules | Predefined read-only count checks on the loaded database; Run target validation; exceptions with four-eyes decision; validation scenarios (Suggest defaults, Save scenario) | After the run |

## Walk findings (honest notes)
- Household preview: 46 source rows because the advisors lookup is joined; REPRESENTATIVE_NAME is blank in all 46 rows, HH_GROUPING_CODE blank in 30, HOUSEHOLD_FIRST_NAME blank in 16. The advisor-file proposals were rejected, so ADVISOR_NAME has no source. This is an open modelling decision.
- Client ADDRESS_LINE_2 blank in all 40 rows (data, not a mapping fault).
- Five target fields have no canonical source (OPEN_CLOSED, PHONE_HOME, HOUSEHOLD_DOB, HOUSEHOLD_LAST_NAME, ORION_HOUSEHOLD_ID); rejected with a note, no change request raised.
- The Household SQL header names the schema as dbo while the Azure SQL target is the simplicity schema; check the generated SQL before relying on it.
- Entity-level approval of Household reported advisors as needing individual review even after the file-level approval; the file-level approval is what counted.
- Not browser-tested in this walk: Deal Room, tenant, command-center template pages, Firm knowledge uploads, Confluence import.

## Setup and knowledge walk (same workspace)
| Element | Action and result |
|---|---|
| Firm knowledge, Attach Data Asset (category + file; .md .txt .csv .tsv .json .xlsx .xlsm .xls .docx .pptx .pdf) | Uploaded `corecap_business_rules.md` as business_rules (registered as DOC-001) and a source-analysis note `corecap_migration_decisions.md` (a shortened copy of the demo note: source meaning, derivations, missing values; the open-decisions and extract-quality sections were left out) |
| Acquisition questionnaire | Saved firm CoreCap, source systems, data owner and mapping approver, and the identifier and relationship notes (HouseholdId, AdvisorId links; no account-to-contact link) |
| Import Confluence page | Not exercised: no page link supplied |
| Business rules, Infer draft rules from asset | Asset DOC-001 gave 5 draft rules (2 value maps, date format, number clean, concat); all 5 approved with a comment, each pins as RULE:ID@1 |
| Azure setup, Test connection (Data Factory sign-in) | Refused: tenant ID and client ID of a service principal are not set. Not needed while the run is started by hand in ADF |
| Azure SQL, Test SQL connection | Passed: schema simplicity exists with ORION_MCL_ACCOUNT, ORION_MCL_CLIENT, ORION_MCL_HOUSEHOLD, xodus_run_log |
| GitHub, Test connection | Passed: signed in and can write to xodus-amp main |
| Transformation SQL page | Shows `[dbo]` table names; the Azure SQL schema is simplicity. Not yet checked against the published package |
