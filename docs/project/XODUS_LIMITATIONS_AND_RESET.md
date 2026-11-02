# XODUS POC: limitations, reset procedure and demo rehearsal

## Known limitations (state these in the demo)
- Source to canonical to target mapping only. No reverse mapping, no production load, no wave or delta assignment.
- The target (Simplicity) is a **simulated** local service. Azure, ADF and storage values in the ADF zip are fictitious; nothing is deployed or sent.
- Execution is **SIMULATED**: counts and rule-linked problems come from an in-memory run of the package. No record values are stored in telemetry.
- The deployment package (`package.yaml` v0.1-DRAFT) is a local draft contract, not agreed with the technical owner.
- Identity is demo accounts only (git-ignored password hashes); no identity-provider integration.
- Rule inference understands seven sentence patterns; anything else is listed as "not recognised", never guessed.
- Relationship candidates are evidence only; confirming one does not join or merge files.
- Source drift only detects an approved mapping whose source field is missing from the current files; it does not compare types or values.
- Account files with household-link columns can be proposed oddly; approve them individually when entity-level approval is blocked.

## Reset procedure
Use the smallest reset that works.
1. **New migration, keep history:** Migration Setup, "Start new migration". Earlier migrations stay for audit; source files, mappings and results start empty. The confirmed target stays (it is platform-level).
2. **Re-run the demo:** after step 1, "Load demo data set". Confirm each entity; if Account is blocked, approve its files individually.
3. **Reset the simulated target:** stop the target service, move `simulated_target_service/data/` aside, start the service again, then re-infer and re-confirm on Target system & mapping (or use Reopen setup).
4. **Full local reset (last resort):** stop the app, move `App_Data/` to a dated backup folder (do not delete it), restart. Demo logins keep working because hashes live in `demo_data/demo_passwords.local.json`.

## Demo rehearsal (about 10 minutes, Platform Admin)
1. Sign in; Target system & mapping: Save & test, infer, confirm.
2. Migration Setup: Load demo data set.
3. Source discovery: confirm Household and Client; approve Account files individually; Profile approved files & generate mappings; approve each entity's proposals.
4. Knowledge assets: show the business-rules asset; Transformation rules: infer drafts from it, approve one.
5. Target system & mapping: refresh proposals, approve with a note, show coverage, generate the package and the ADF zip.
6. Validation: suggest default scenarios, adopt, run validation, propose and decide an exception (two different people), download sign-off evidence.
7. Run simulated execution; acknowledge a problem and resolve it with a reason.
Verified so far in headless Chromium against a local copy (unstyled, because the CDN stylesheet was blocked there); a rehearsal in the real browser with styling is still to be done.
