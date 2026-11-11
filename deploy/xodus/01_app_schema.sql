-- The platform's own tables (users, approvals, mappings, events, packages) in the XODUS database, schema xodus_app, apart from the
-- data-plane schemas (xodus_raw, xodus_canon, xodus_mirror, xodus_ops, xodus_ref). Run once in a connection to the XODUS database as
-- the server admin, after replacing the password in angle brackets. Do not commit the real password.
--
-- xodus_app_user is a contained database user whose default schema is xodus_app: the app's unqualified table names land there.
-- It may create tables and read and write them in xodus_app only. Its connection string goes into the app's settings as
--   DATABASE_URL=mssql://xodus_app_user:<password>@<server>.database.windows.net:1433/<XODUS database>
-- (URL-encode special characters of the password).

IF SCHEMA_ID(N'xodus_app') IS NULL EXEC(N'CREATE SCHEMA [xodus_app]');
GO

IF USER_ID(N'xodus_app_user') IS NULL
    CREATE USER [xodus_app_user] WITH PASSWORD = N'<strong password>', DEFAULT_SCHEMA = [xodus_app];
ELSE
    ALTER USER [xodus_app_user] WITH DEFAULT_SCHEMA = [xodus_app];
GO

GRANT CREATE TABLE TO [xodus_app_user];
GRANT ALTER, SELECT, INSERT, UPDATE, DELETE, REFERENCES ON SCHEMA::[xodus_app] TO [xodus_app_user];
GO
