# Hosting XODUS in Azure

The platform runs as one container in Azure Container Apps, and keeps its own data (users, approvals, mappings, code values, events,
packages, stored files) in the XODUS Azure SQL database, schema `xodus_app`. The local PostgreSQL container is no longer needed once
the data is copied. The data plane (`xodus_raw`, `xodus_canon`, `xodus_mirror`, `xodus_ops`, `xodus_ref`) is unchanged.

Decided 2026-11-11: Container Apps; copy the existing PostgreSQL data; secrets as Container Apps secrets (app settings).

## 1. The platform's schema and database user (once)

Run `deploy/xodus/01_app_schema.sql` in the XODUS database as the server admin, after replacing the password placeholder.
It creates schema `xodus_app` and the contained user `xodus_app_user` (default schema `xodus_app`; may create tables and read and
write only in that schema).

## 2. Copy the platform's data from the local PostgreSQL (once)

Allow your machine's IP on the SQL server firewall, then, with the local stack running:

    docker compose exec control_plane python scripts/copy_platform_db_to_xodus.py --target "mssql://xodus_app_user:<password>@<server>.database.windows.net:1433/<database>"

It creates every platform table with the app's own definitions, then copies the rows. It prints table names and counts only, and
never writes into a table that already holds rows unless `--replace` is given.

Optional check before Azure: set `DATABASE_URL` in `.env` to the same `mssql://` URL, restart the local stack and open
`http://127.0.0.1:8080/healthz`; it reports `"database": "mssql"`.

## 3. Deploy the container

    copy deploy\azure\app.settings.env.example deploy\azure\app.settings.env   (fill it in; it is git-ignored)
    az login
    powershell -File deploy/azure/deploy_container_app.ps1 -ResourceGroup <rg> -Location <region> -Prefix xodus

The script builds the image in a container registry, creates the Container Apps environment and the app (external HTTPS, exactly
one replica, because sign-in sessions are held in memory), and stores every setting as a secret. It prints the app's address and
its outbound IP addresses.

## 4. After the deployment

- Azure SQL server, Networking: allow the app's outbound addresses (or "Allow Azure services and resources to access this server").
- Storage account, Networking: allow the same addresses (the platform reads and writes the lake).
- Open `https://<app>/healthz` (`"database": "mssql"`), sign in, and check Platform setup: Azure connection, GitHub, target system.

## Notes

- The adapter (`control_plane/mssql_compat.py`) translates the platform's SQL to T-SQL: tables, upserts (MERGE), LIMIT, and
  case-sensitive text columns as in PostgreSQL. Key and unique text columns are bounded (index keys are limited to 900 bytes:
  450 characters for a single-column key, 225 for two columns, 150 for three).
- Index definitions are skipped on Azure SQL for now; the platform's tables are small.
- Exactly one replica: running more would need a shared session store first.
