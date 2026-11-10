-- Access to the XODUS database for the stable XODUS pipelines. Run once in a connection to the XODUS database as a Microsoft Entra
-- admin of the server, after replacing the two names in angle brackets. No passwords are kept here.
--
-- Data Factory (managed identity): the setup pipeline creates and replaces the XODUS objects (xodus_raw, xodus_canon, xodus_mirror,
-- xodus_ops) and the run pipelines write data and run the generated procedures.
-- XODUS platform login: reads everything (run log, counts, catalog) and may INSERT into one table only, the promotion gate.

IF USER_ID(N'<adf_factory_name>') IS NULL
    CREATE USER [<adf_factory_name>] FROM EXTERNAL PROVIDER;
ALTER ROLE db_datareader ADD MEMBER [<adf_factory_name>];
ALTER ROLE db_datawriter ADD MEMBER [<adf_factory_name>];
ALTER ROLE db_ddladmin ADD MEMBER [<adf_factory_name>];
GRANT EXECUTE TO [<adf_factory_name>];
GO

-- The promotion gate table, created here so the platform's INSERT can be granted before the first setup run (same definition the
-- setup pipeline uses; it never drops it).
IF SCHEMA_ID(N'xodus_ops') IS NULL EXEC(N'CREATE SCHEMA [xodus_ops]');
GO
IF OBJECT_ID(N'xodus_ops.xodus_promotion_gate', N'U') IS NULL
    CREATE TABLE [xodus_ops].[xodus_promotion_gate] ([gate_id] bigint IDENTITY(1,1) NOT NULL PRIMARY KEY, [migration_id] nvarchar(64) NOT NULL,
        [batch_id] nvarchar(64) NOT NULL, [validation_run_id] nvarchar(64) NOT NULL, [signed_off_by] nvarchar(256) NOT NULL,
        [signed_off_utc] nvarchar(64) NOT NULL, [counts_json] nvarchar(max) NOT NULL, [written_utc] datetime2 NOT NULL DEFAULT SYSUTCDATETIME());
GO

IF USER_ID(N'<xodus_sql_login>') IS NULL
    CREATE USER [<xodus_sql_login>] FOR LOGIN [<xodus_sql_login>];
ALTER ROLE db_datareader ADD MEMBER [<xodus_sql_login>];
GRANT INSERT ON [xodus_ops].[xodus_promotion_gate] TO [<xodus_sql_login>];
GO
