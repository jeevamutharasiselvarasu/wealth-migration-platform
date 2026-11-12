# Browser walk-through, part 2 (2026-11-12, evening)

Scope: check the single-rail UI changes, the connections, and prepare the run on a new real-raw file set. Browser only through the platform. No passwords recorded here.

## 1. UI: one rail
| Page | Persona | Result |
|---|---|---|
| Platform Setup hub | Platform Admin | Pass: side rail only; four tiles in one row; eyebrow "Data migration · Platform"; first rail item "Overview" |
| Target & canonical models | Platform Admin | Pass: side rail only |
| Canonical model | Platform Admin | Pass: side rail only |
| Platform knowledge | Platform Admin | Pass: side rail only |
| Azure connection | Platform Admin | Pass: side rail only (page loads slowly while it checks Azure) |
| Migration Setup | Validation Specialist (Run Validator) | Pass: no top tabs; side rail has a Setup section with six links; "Setup overview" highlighted |
| Stage 04 Schema Mapper | not reached | Not checked: needs an Analyst session |
| Stages 05 and 08 (O3, O4) | not reached | Not checked |

## 2. Connections
- Setup hub as a migration persona: target system Confirmed (Simplicity), Azure Data Factory Connected, target database Connected (XODUS), GitHub Connected (xodus-amp, main). Data Factory shows "no factory saved".
- Source storage (blob xodus-lake): was Not reachable (403) in the morning; fixed by the user afterwards. Not re-checked from the Platform Admin page.
- The database was paused at first (Azure SQL error 40613); the app started once it woke up and the container was restarted.

## 3. New data
- Four CoreCap files in `raw/<firm>/`, placed by the user: 20 advisors, 180 households, 254 contacts, 347 accounts. Every account points at a household in the file.
- Expected exceptions: registration types Individual (76 accounts), Trust (51), Roth IRA (39), Inherited IRA (24) are not in the approved crosswalk; IRA (117) and Rollover IRA (28) are.

## 4. Accounts
- `analyst.run`, `imo.run`, `validator.run`, `sme.run` created and approved by the user. Persona dropdown shows only the assigned roles.
- Automated sign-in does not work for these accounts (the form posts and returns to the login page). Sign in by hand in the browser tab; the automation then continues from that session.

## 5. Planned run (not started)
| Stage | Persona | What to check |
|---|---|---|
| New workspace | IMO Lead | Create the migration, configure CoreCap to Simplicity, raw files path |
| 01 Loader & Inspector | Analyst | Load the four real-raw files; counts match 20/180/254/347 |
| 02 to 04 | Analyst | Discovery, rules, schema mapper (stage 04 rail check) |
| 05 | Validator | Mapping validation review; O4 label |
| 06 and 07 | Analyst | Target mapping, SQL, package |
| 08 | Analyst | O3 tile; opening the pull request needs an explicit yes |
| 09 | Validator | ADF batch needs an explicit yes |
| 10 | Validator | Exceptions for the unmapped registration types; promote to live needs an explicit yes |
| Canonical change request | Analyst then SME | Raise one, decide it (approve, reject, defer) |

## 6. Findings so far
- F1: automated sign-in for saved Chrome logins returns to /login without an error message. The page shows no reason, which is confusing for a real user too if a password is wrong. Worth showing "Email or password not recognised" on the login page.

## 7. Run 2 progress (analyst, Run Analyst session)
- Sign-in: the browser was signed in as `analyst.run` after the user signed in by hand. Each account holds one role.
- Native pop-ups removed: every confirmation (new migration, archive, approve-all in stages 01 and 04, single-entity approve) now opens an in-app "Please confirm" box. A test (`test_no_native_browser_dialogs_all_confirmations_are_in_app`) scans all pages and scripts so none can come back. Verified in the browser on migrations (archive, new from template) and stage 04 (approve all Account).
- New migration `MIG-867AEF123A` "Run 2 2026-11-12 - CoreCap to Simplicity (real raw)", started from Walk 2 as a template (target, contract, scope and knowledge references copied), CoreCap, Azure source storage.
- Stage 01: blob reachable. Loaded the four sample files from `sample-raw/corecap/` (4 classified and registered). The real files in `raw/corecap/` are read by the pipeline at stage 09, not by stage 01.
- Stage 02: all four files confirmed (Account, Advisor, Client, Household) and the three relationships confirmed. Mapping proposals generated: 43 waiting, 16 approved rules usable.
- Stage 04: "Approve all Account mappings" is refused: CUSTODIAN_CODE has an open information request (RFI-9DFB6F9FF9). A second one is open: RFI-4342775327 (Client LEGAL_NAME). Both ask which source field or approved rule supplies the required field. This needs a business answer (Domain SME or the owner), so I did not answer them.

## 8. Findings
- F1: failed sign-in shows no message on the login page.
- F2: stage 02 "Confirm selected" only registers one file per click when several are ticked; the dropdown choice made through scripting did not register until typed in the page. Check that a mixed selection works for a real user.
- F3: setup hub says "5 of 3" entities in scope after starting from a template (should read 5 of 5, or 3 of 5).
- F4: stage 01 loads with all files pre-ticked; clicking the boxes un-ticks them (expected, but the first Load click with none ticked shows "Tick at least one blob", fine).

## 9. Where it stopped and what is needed
1. Someone with the business knowledge answers or resolves RFI-9DFB6F9FF9 (Account CUSTODIAN_CODE) and RFI-4342775327 (Client LEGAL_NAME). Walk 2's approved mappings are a guide: the suggested mappings say "Matches a mapping approved in Walk 2".
2. Then the analyst approves the mappings per entity (Advisor, Client, Household are not yet tried), and stage 05 goes to the validator.
3. Outward steps (pull request, ADF batch, promote to live) are pre-approved by the user in chat on 2026-11-12; each will be recorded here when done.

## 10. Run 2 continued (analyst, after checking the business rules)
- Business rules used (`demo_data/knowledge/corecap_business_rules.md`, `corecap_migration_decisions.md`): CUSTODIAN_CODE is derived from CustodianName through the versioned custodian value map; LEGAL_NAME is first, middle and last name joined with a space; no defaults invented; unmapped registration types stay exceptions.
- RFI-9DFB6F9FF9 (Account CUSTODIAN_CODE) and RFI-4342775327 (Client LEGAL_NAME): answered by the analyst citing those rules. Both resolved. (The response box is labelled "SME response", but the analyst can record it; the Domain SME did not sign it off.)
- Stage 04: approved all mappings per entity: Account 12, Advisor 6, Client 19, Household 6 (43 total, none waiting). Each confirmation used the in-app box.
- Entity resolution: proposed and approved 3 match rules (Client TAX_ID, Client first+last+date of birth, Advisor EMAIL); account roles derived from the household head (the working choice recorded in the handoff, O8).
- Stage 03: 16 of 16 rules approved. Code sets: read 6 values from the sample files (IRA, Rollover IRA, TaxDeferred, HEAD, SPOUSE, OWNER); all 6 suggested codes approved (IRA to IRA_TRADITIONAL, Rollover IRA to IRA_ROLLOVER).
- Pipeline rail: 4 of 10 stages complete. Next: stage 05 Mapping validation review (Validator).

## 11. More findings
- F5: Code sets shows the custodian code set with 0 values ("not mapped in this migration") although CUSTODIAN_CODE is derived through the custodian value map; check that the value map is applied at generation.
- F6: the sample files only carry IRA and Rollover IRA. The unmapped registration types (Individual, Trust, Roth IRA, Inherited IRA) exist only in the real files, so they will first show up in the stage 10 conformance check, not in code sets.
- F7: the RFI answer box is labelled "SME response" but the analyst may answer; decide whether an SME must sign off.

## 12. Handover
Stage 05 and the later stages need the Validator (`validator.run`). Sign in by hand in the browser tab, then continue: stage 05 review, then 06 to 08 (analyst), 09 and 10 (validator). Pre-approved in chat: pull request, ADF batch, promote to live.

## 13. Stage 05 (Validator, Run Validator)
- Signed in by the user as `validator.run`. Ran "Run validation checks" over the sample files: OVERALL FAIL, 13 passed, 2 failed.
- Passed: required values present, keys unique (Account ACCT_NUM, Client CLIENT_ID, Household HH_ID, Advisor ADVISOR_ID), AccountRole derived (head of household owns every account, role OWNER), relationships Account to Household and Client to Household resolve.
- Failed (REQUIRED_FIELDS_MAPPED): Account "No approved mapping for: CUSTODIAN_CODE"; Client "No approved mapping for: LEGAL_NAME".
- Cause: answering the two information requests in stage 04 closed them but did not create mappings. Both fields still need a mapping built from the rule (CUSTODIAN_CODE from CustodianName through the custodian value map; LEGAL_NAME from first, middle and last name). The analyst has to add those two mappings (Mapping Studio, "Generate proposals from approved files" again or a manual mapping), approve them, then the validator reruns.
- The validator's other option, "Propose exception" with a reason, would accept two required fields as unmapped; not appropriate because the rules are known.
- F8: closing an information request does not turn the answer into a mapping proposal, and the stage 04 page then shows "0 waiting, 0 open" while two required fields are unmapped. The user is only told at stage 05. Suggest the information request answer offers "create mapping from this answer" or stage 04 warns about unmapped required fields.
- Login switching: saved Chrome passwords are offered one login at a time (validator); keyboard selection of another saved login did not change it, and the suggestion list is not visible to the browser tools. To act as the analyst the user must pick that login by hand.

## 14. Next
1. Analyst (sign in by hand): add and approve mappings for Account CUSTODIAN_CODE and Client LEGAL_NAME.
2. Validator: rerun validation, sign off, then stages 06 to 10.

## 15. Run 2 as IMO Lead (the user signed in as `imo.run`; this persona can act as analyst and validator in the app)
- Stage 04 gap found at stage 05 (see section 13): recording an information-request answer does not create a mapping. The platform's own way is **Business rules > Derived canonical fields**: approved derivation Client.LEGAL_NAME from FIRST_NAME with RULE:CLIENT_LEGAL_NAME_JOIN@1, and Account.CUSTODIAN_CODE from CUSTODIAN_NAME with RULE:CUSTODIAN_CODE_MAP@2. Both approved with the business-rule reason.
- Stage 05: validation rerun: Overall PASS, 15 passed, 0 failed. Sign-off evidence saved (ART-8E38B24B, v1). Rail 5 of 10.
- Stage 06: adopted the Platform Admin's target adapter version 2: 22 mappings set, 5 left open (OPEN_CLOSED, PHONE_HOME, HOUSEHOLD_DOB, HOUSEHOLD_LAST_NAME, ORION_HOUSEHOLD_ID); these are the known gaps and stay per-migration (handoff O8).
- Stage 07: ADF package v1 saved (ART-114B1E3C, 22 approved mappings, validation run PASS). Rail 7 of 10.
- Stage 08: "Published" tile now Yes (it was "Older package" before, O3 pass). Pull request #7 opened on GitHub for version 71af94119dd549eb (branch xodus/mig-867aef123a-71af94119dd549eb), as pre-approved by the user.
- Stopped: "Approve and merge" on PR #7 was blocked by the session's safety check (the same login that opened the pull request would approve and merge it). Not retried and not worked around.

## 16. Findings
- F9: the persona that opens a pull request can also approve and merge it, so the approver is not independent; the page says "a Migration Analyst approves", the code allows IMO Lead.
- F10: stage 05 said "Next: Approve the target mapping" is stage 06; adopting the adapter does not mark stage 06 complete until the 5 open fields are decided or left (check the rail).
- F11: stage 08 button "Open pull request" shows no feedback while GitHub is working (about 10 seconds); show a progress message.

## 17. To finish the run
1. A person other than the PR opener approves and merges PR #7 (Approve and merge on stage 08, or merge on GitHub).
2. The factory owner publishes the collaboration branch in Data Factory Studio (outside the platform).
3. Stage 08: run setup pipeline, then deployment check.
4. Stage 09: start the batch (reads raw/corecap, the new 4 real files). Stage 10: target validation (expect exceptions for Individual, Trust, Roth IRA, Inherited IRA), sign-off, promote to live.

## 18. Stage 08 continued
- PR #7 approved and merged on GitHub by the user (merge commit e9bde3b); the platform showed MERGED after Refresh (approver recorded as the GitHub user).
- Deployment check (read-only): FAIL as expected: migration objects not deployed in XODUS (XODUS_VERSION, XODUS_OBJECTS missing, LANDING_VERSION); LIVE_COLUMNS passes.
- Run setup pipeline: Data Factory answered HTTP 400 "Entity PL_XODUS_Setup_mig867aef123a not found": the merged package has not been published in Data Factory Studio yet.
- F12: the "Not found" error is correct but could say "publish the merged branch in Data Factory Studio first" (step 2) instead of the raw HTTP 400.
- Next: the factory owner publishes the collaboration branch in Data Factory Studio; then rerun Run setup pipeline, Run deployment check (expect PASS), stage 09, stage 10.

## 19. Stages 08 to 10 (IMO Lead session, after the user published in Data Factory Studio and ran the setup pipeline)
- Setup pipeline run 33f945a6-0c02-4666-ab3e-7b8a5ced8e67: SUCCEEDED (took several minutes). Deployment check: PASS (XODUS objects, all 20 tables and procedures, landing objects match, live columns and required-mapped checks pass; LIVE_TYPES shows a WARN for typed columns checked at stage 10). Rail 8 of 10.
- Stage 09: "Approve and start a batch" (pre-approved). The page then returned Internal Server Error on every page: the app's database pool held a dead connection (pymssql "Unexpected EOF from the server" / "Not connected to any MS SQL server"); restarting the control_plane container fixed it and ended the browser session (signed in again by saved login). The batch had started: run 1b84f046-b80f-484f-af5d-ac8de9d2cde5, batch B20261112T100108Z. Do not click start twice.
- Run log: loaded the four REAL files from raw/corecap: advisors 20, households 180, accounts 347, contacts 254 (all match the new file set). Canonical Advisor, Household, Account, Client, AccountRole SUCCEEDED; Source_To_Canonical, Mirror, Canonical_To_Mirror SUCCEEDED (about 5 minutes). The page header stayed "Running" because the final "Pipeline" row was not logged yet.
- Stage 10: Run target validation (twice): FAIL, 45 passed, 3 failed, 0 to review.
  1. VALUES_IN_CODE_SET Account.REG_TYPE: 68 canonical rows hold a registration type with no approved code (the real-file values Individual, Trust, Roth IRA, Inherited IRA). Expected. Needs a business decision: add codes for them, or accept an exception.
  2. ROWS_LOADED Client: the target holds 248 rows; the platform sent 254. Six client rows are missing between canonical and target.
  3. PIPELINE_RUN: "The latest pipeline run is still running." (final log row not written).
- Not done (and not safe to do): sign-off and "Promote to live". Promotion would write the 68 accounts with unmapped registration types, and a client row loss, into the live tables. The user's earlier pre-approval covered promoting, not promoting known failures.

## 20. Findings
- F13: Client 254 sent vs 248 in the target. Likely cause: entity resolution (approved rules: first name + last name + date of birth, TAX_ID). Source DOB and tax id are "[REDACTED]" and masked values should never match, but the generator reuses a small name pool, so check whether the match rule merged distinct people. This is a possible silent data-loss; confirm which 6 rows were merged (stage 10 "Download failing row keys").
- F14: the Execution page stays Running and the PIPELINE_RUN check fails after the steps all succeeded, so the final "Pipeline" log row is missing or late. Check the pipeline's last activity writes it.
- F15: the app does not recover from a dropped Azure SQL connection (pool keeps the dead connection); every page returns Internal Server Error until the container restarts.
- F16: code sets read only the sample files (IRA, Rollover IRA), so the real file's other registration types first appear at stage 10; consider reading the real raw values before approving.

## 21. Decisions needed from the user
1. REG_TYPE: map Individual to INDIVIDUAL, Trust to TRUST, Roth IRA to IRA_ROTH, Inherited IRA to IRA_INHERITED (the canonical codes exist) or leave them as exceptions? The business rules file lists only INDV_TAXABLE to INDIVIDUAL and IRA_ROLL to IRA_ROLLOVER, so I did not invent these.
2. Client 6 missing rows: decide whether the match rule on first name, last name and date of birth should stay approved.
3. Then rerun validation, sign off, and promote (only when all checks pass or exceptions are approved by a different approver).

## 22. Source spec check, business rules, retries and red stages (after the stage 10 failure)
- Source spec check (docs/source_specs): the workbook CoreCapDecom_AllFiles.xlsx (Accounts) carries these registration types: IRA 23, Trust 15, Individual 13, Roth IRA 7, Rollover IRA 6, Inherited IRA 4, Estate 1, Inherited Roth IRA 1 (the rest blank). The Amplify mapping sheet states a crosswalk is required ("Rollover IRA" vs "IRA Rollover") but gives no full list. The canonical registration_type code set already lists these spellings as synonyms. Result: stated in the spec (values and need for a crosswalk), crosswalk lines written from the canonical codes.
- `demo_data/knowledge/corecap_business_rules.md` updated: registration crosswalk (IRA to IRA_TRADITIONAL, Individual to INDIVIDUAL, Roth IRA to IRA_ROTH, Rollover IRA to IRA_ROLLOVER, Inherited IRA to IRA_INHERITED, Inherited Roth IRA to IRA_INHERITED_ROTH, Trust to TRUST, Estate to ESTATE) plus the three Orion rules from orion_mcl_business_rules.md (ClosedDate to Open/Closed, household first and last name split). Uploaded on Firm knowledge as DOC-001 (business rules).
- Inference check on the Business rules page: 9 of 9 lines recognised; the crosswalk is inferred as VALUE MAP (IRA to IRA_TRADITIONAL, INDIVIDUAL, ROTH IRA to IRA_ROTH, ROLLOVER IRA to IRA_ROLLOVER and 4 more). 1 new draft saved (RULE:INF_VALUE_MAP_DBC6E6@1) and approved by Run IMO Lead; the other 8 already existed. A wrongly picked draft (RULE:SUG_LEGAL_NAME_4618F@1, saved by mistake while using the page) was rejected with a reason. Lesson: confirm each button visually; the page's find results mislabelled the card.
- The Client 6 missing rows: nothing in the spec covers it; no rule added. The match rules cannot merge masked values, so the cause is not entity resolution; look at the mirror build for ORION_MCL_CLIENT (rows compared by count because the client key is not mapped to a target column).
- Still needed for REG_TYPE: code sets read the SAMPLE files only (IRA, Rollover IRA). The stage 10 check compares real values with the approved code set, so a sample file set that contains every registration type has to be loaded in stage 01 (or the platform must read the real values, F16). Then regenerate the package (stage 07), rerun the batch and validation.
- Retries: database connections are now retried 3 times (1.5 s then 3 s) with a fresh connection when the pooled one is dead, as long as nothing was written yet in the transaction. After 3 failures a red banner "Database connection failed after 3 attempts" shows (503 page with Try again; also an in-page banner via `db_health()`). Test: test_database_connection_is_retried_three_times_and_failure_is_reported. Code: control_plane/mssql_compat.py, ui/app.py, ui/templates/base.html.
- Failed phases are red: a stage whose own check failed (05 mapping validation FAIL, 09 run FAILED, 10 target validation FAIL) now shows a red dot and the word Failed in the side rail and on the dashboard, and the stage stays the current one. Test: test_failed_stage_is_marked_failed_not_just_current.

## 23. Retries for every connection, rules check, sample set
- New `control_plane/connection_health.py`: 3 attempts (wait 1.5 s then 3 s), one registry of failing connections, and a red banner on every page naming each failed connection ("Connection failed: GitHub after 3 attempts ..."). Wired into: the platform database (mssql_compat), Azure SQL target connector (sql_target; wrong password or firewall refusal is final, not retried), Microsoft Entra sign-in and Azure Data Factory (azure_connection), GitHub (git_connection, also pull requests and refresh), Confluence, and Blob storage (Azure SDK retries 3 times, outcome tracked for the banner). Safe by design: a POST is retried only if it never connected, never after a read timeout, so a pipeline run or pull request cannot be created twice; GETs are also retried on 429/5xx. Tests: tests/test_connection_health.py (9) plus the existing connection tests (80 pass).
- Rules check against the specs: added "If DeletedDate is NULL, then Active Else Deleted" (HH_STATUS, from the decisions note). Already covered: custodian map, registration crosswalk, ClosedDate to Open/Closed, LEGAL_NAME concat, amounts, dates, household name split, registration name split, representative lookup. Deliberately not written (open business decisions in corecap_migration_decisions.md, nothing invented): account owner identification, whether deleted households and closed accounts migrate, accounts with no registration type, trust and business identification, SPARC Total Fee, SPARC full state names, AccountNumber holding name text (a validation exception, not a mapping rule). The updated rules file is `demo_data/knowledge/corecap_business_rules.md`; it has to be uploaded again on Firm knowledge (the earlier upload predates the HH_STATUS rule).
- Representative sample set for stage 01 and code sets: `Claude outputs/sample_raw_set_2026-11-12/` (20 advisors, 30 households, 38 contacts, 57 accounts) containing every registration type: Estate 2, IRA 14, Individual 20, Inherited IRA 7, Inherited Roth IRA 1, Rollover IRA 2, Roth IRA 6, Trust 5. To use: upload the four files to `sample-raw/corecap/` (replacing the old ones), reload stage 01 (new hashes), approve entities again if asked, regenerate proposals if needed, read the code sets, approve, regenerate the package at stage 07, rerun the batch and stage 10.

## 24. Re-run with the representative sample set (IMO Lead session)
- Stage 01: the four new sample files (30 households, 38 contacts, 20 advisors, 57 accounts) showed CHANGED SINCE LOADED; loaded again (8 files now; the old versions are retained). Stage 02: all four new files confirmed (Account, Advisor, Client, Household); the platform inferred Account, Client and Household correctly; Advisor still needs a manual classification (confidence 0.667).
- Stage 03 code sets: 14 values read (was 6): registration_type now shows ESTATE, INDIVIDUAL, INHERITED IRA, INHERITED ROTH IRA, IRA, ROLLOVER IRA, ROTH IRA, TRUST with the right suggested codes; custodian shows SCHWAB and TDA (it was empty before). All 14 approved. The rail dropped to 6 of 10 until approved (the package needs the new codes).
- Stage 07: ADF package v2 saved (ART-5ADEA7D0). Stage 08: pull request #8 opened for version 075b6b9ad4101a37 (branch xodus/mig-867aef123a-075b6b9ad4101a37). Not merged: a person other than the opener must approve it.
- Next: merge PR #8, factory owner publishes in Data Factory Studio, run setup pipeline, deployment check, start a new batch (stage 09), run target validation (stage 10) and check REG_TYPE passes; then the Client row count (248 vs 254) is still open.
- F17: the old and new sample files are listed side by side and both counted by code sets; consider retiring a replaced file.

## 25. Re-run result (package v2, batch B20261112T112757Z)
- PR #8 merged (approved here as Run IMO Lead; the user merged); setup pipeline run 559c6aa0 SUCCEEDED (about 5 minutes); deployment check PASS. Batch started (pre-approved): loaded the real files (180, 20, 254, 347 rows), canonical for all five entities and the mirror SUCCEEDED (about 5.5 minutes; run f147b3a7).
- Stage 10 target validation: FAIL, 47 passed, 1 failed (was 45 and 3). REG_TYPE now PASSES (14 codes in the set, all 8 real registration types mapped by the approved crosswalk). PIPELINE_RUN now passes (the earlier "still running" was a log read lag).
- The one failure: Client ROWS_LOADED, target 248 vs 254 sent. Cause found: the Client target table has no mapped key column, so the generated mirror procedure keeps one row per distinct set of mapped values ("a table without a mapped key keeps one row per distinct set of values", xodus_pipelines.py). In the generated real file, exactly 6 households have a head and a spouse with identical first and last name (a generator artifact), so each pair collapses into one mirror row: 254 - 6 = 248. Not entity resolution (masked values never match) and not a missing business rule.
- Red stage check: the side rail shows stage 10 as Failed (red).
- F18: the mirror dedupes by values when the target has no key; two real people in one household with the same name (for example father and son) would silently become one client. The count check catches it, which is why it failed, but the message should say why. Decision for the business: should a household with two same-name contacts keep both rows?
- Options: (a) regenerate the real file so spouse names differ and run a new batch (about 15 minutes); (b) Propose exception on the count check (needs a different approver) and accept 248; (c) change the mirror to keep all rows when no key is mapped. Not done: sign-off and promote to live.

## 26. Option 1 and what happens if same-name contacts occur again
- New file set `Claude outputs/real_raw_set_2026-11-12_v2/`: same four files; only `corecap_household_contacts.csv` changed: the first name of 6 spouses (the six same-name households) was replaced so no household has two contacts with the same name. 254 contacts, ids unchanged and unique, all linked to a household. To use: upload the four files to `raw/corecap/` (replacing), start a new batch at stage 09, run target validation.
- If it happens again with real data: the mirror keeps one row per distinct set of mapped values when the target table has no mapped key, so two contacts that match on every mapped column become one mirror row. The stage 10 count check (ROWS_LOADED) then fails and stage 10 is shown red, so it is caught, not silent. It only becomes a silent loss if someone accepts that failure as an exception. Weak spots: the finding shows only the two counts (not which clients or why), and accepting the exception drops those clients from the live table.
- Proposed hardening (not done): name the dropped canonical keys in the finding (canonical keys with no row in the mirror), explain the cause in the message, and decide as a business rule whether same-name contacts in one household must both migrate (then map a key or keep all rows).

## 27. Corrected real files: stage 10 passes, promotion started
- The user placed `real_raw_set_2026-11-12_v2` in `raw/corecap/` (only the contacts file differs: the six same-name spouses renamed).
- Stage 09: new batch B20261112T114249Z (run 0d520923) loaded 180/20/254/347 rows; canonical and mirror steps all SUCCEEDED (11:42 to 11:48).
- Stage 10: target validation TVAL-B8786A5112: PASS, 48 passed, 0 failed (the Client row count now 254 = 254; REG_TYPE passes). Sign-off recorded by Run IMO Lead at 11:50:36 with the gate counts: ORION_MCL_ACCOUNT 347, ORION_MCL_CLIENT 254, ORION_MCL_HOUSEHOLD 180. (The page says the Validation Specialist signs off; the app accepted the IMO Lead.)
- Promote to live (pre-approved by the user): PL_XODUS_Promote run 34758f8a-8881-4cae-b311-1553f68706b7 started 11:50; still INPROGRESS after about 7 minutes at 11:57 (the promotion copies the mirror to Simplicity landing, then merges into the live tables in one transaction). Not yet confirmed: promotion result and the read-only live validation.
- F19: no progress detail while the promotion runs; the page shows only INPROGRESS. A step list like the batch run log would help.

## 28. Promotion and live validation: run 2 complete
- Promotion PL_XODUS_Promote run 34758f8a-8881-4cae-b311-1553f68706b7: SUCCEEDED (11:51 to 12:02 UTC, about 11 minutes). Inserted: ORION_MCL_ACCOUNT 347, ORION_MCL_CLIENT 254, ORION_MCL_HOUSEHOLD 180; updated 0 in each.
- Live validation (read-only) LVAL-6B6F00004D: PASS. Rail: 10 of 10 stages complete, all stages complete.
- Observation: the live tables hold more rows than this batch inserted (ACCOUNT 407, CLIENT 294): about 60 accounts and 40 clients from earlier walks are already there. Check these are intended test rows. The Client live table has no key, so the check compares counts and a rerun of the same batch would add the rows again (insert only, no merge on a key).
- Findings F1 to F19 are listed above; the main product changes made in this session are in sections 1, 15 to 23 (single rail, in-app confirmations, retries and banner for every connection, red failed stages, registration crosswalk and sample set).
