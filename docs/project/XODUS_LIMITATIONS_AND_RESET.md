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
4. **Full local reset (last resort):** stop the app, move `App_Data/` to a dated backup folder (do not delete it), restart. Self-registered accounts live in the database, so a local SQLite reset also clears them: register the admin again.

## Demo rehearsal (about 10 minutes, Platform Admin)
1. Sign in; Target system & mapping: Save & test, infer, confirm.
2. Migration Setup: Load demo data set.
3. Source discovery: confirm Household and Client; approve Account files individually; Profile approved files & generate mappings; approve each entity's proposals.
4. Knowledge assets: show the business-rules asset; Transformation rules: infer drafts from it, approve one.
5. Target system & mapping: refresh proposals, approve with a note, show coverage, generate the package and the ADF zip.
6. Validation: suggest default scenarios, adopt, run validation, propose and decide an exception (two different people), download sign-off evidence.
7. Run simulated execution; acknowledge a problem and resolve it with a reason.
Verified so far in headless Chromium against a local copy (unstyled, because the CDN stylesheet was blocked there); a rehearsal in the real browser with styling is still to be done.


## Accounts on a hosted deployment (Render)
- People register at `/register`. Passwords are stored only as salted scrypt hashes in the control-plane database (`app_users`). No hash variable is needed on the host.
- New registrations are PENDING with no roles. A Platform Admin approves them on `/admin/users` and assigns roles; changing roles or disabling an account ends that user's sessions. The last active Platform Admin cannot be disabled or demoted.
- The bootstrap administrator is the email in `XODUS_BOOTSTRAP_ADMIN_EMAIL`; when that variable is not set, the very first registration on an empty user table becomes Platform Admin (and may load the demo data set). On a public URL, register immediately after deploying, or set the variable so nobody else can claim the first account.
- Built-in demo emails cannot be registered. `XODUS_DEMO_PASSWORD_HASHES` still works for them.
- Persistence: users live in `App_Data/controlplane.db`. On a host with an ephemeral disk (default on Render) they are lost on every redeploy or restart; attach a persistent disk mounted at the `App_Data` folder, or move the control plane to a managed database.
- Not built: email verification, password reset, CSRF tokens, an IP-level rate limit (only a per-account lockout of 5 failures for 15 minutes). Add these before any non-demo use.

## First-start demo seed (2026-11-02)
Set `XODUS_SEED_DEMO=1` to load the demo template automatically when the app starts with no migration (demo mode on). It runs the same loader as "Load demo data set", acts as "System seed", and is recorded in the audit log. It never replaces an existing migration, so it is safe to leave on. Pair it with `XODUS_BOOTSTRAP_ADMIN_EMAIL` so a wiped hosted instance comes back usable after you register the admin. On Render's free tier the target connection may show "not reachable" because the simulated target is not running there.

## PostgreSQL option (2026-11-02)
Set `DATABASE_URL` (for example a free Neon or Supabase connection string, or the Compose `db` service) and the control plane, the event-sourced workspace state, registered users, source uploads and stored artifacts all use PostgreSQL. Without it the app behaves exactly as before on SQLite and local files. Uploaded files and generated artifacts are kept in the `stored_files` table in PostgreSQL mode, so nothing depends on the web host's disk. Use `/healthz` (`database` field) to confirm which backend is live.

Tests: `XODUS_TEST_PG_URL=postgresql://user@host/db pytest` runs the suite against PostgreSQL with an emptied schema per test. Tests that build their own SQLite fixture (marked `sqlite_only`, or using a `db` fixture or `sqlite3.connect`) stay on SQLite in that mode. Not covered: a real Neon/Supabase connection from Render, load with many users, and `docker compose` itself (not run here). `simulated_target_service` still uses its own SQLite file. Backups of a free Postgres are your responsibility.

## Run locally on the laptop (2026-11-02)
Double-click `run_local.bat` (or run it in the repo folder). It creates `.venv`, installs requirements, sets `XODUS_SEED_DEMO=1` and `XODUS_BOOTSTRAP_ADMIN_EMAIL`, and serves http://127.0.0.1:8000. Register the bootstrap email at `/register` once; data stays in `App_Data` (SQLite) between runs. Use `docker compose up --build` instead for PostgreSQL in a container (needs `POSTGRES_PASSWORD` in `.env`). To start clean, stop the app and move `App_Data\controlplane.db` aside.
