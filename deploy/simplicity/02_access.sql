-- Access to the Simplicity live database. Run in a connection to Simplicity as a Microsoft Entra admin of the server.
-- Replace the two names in angle brackets first. No passwords are kept here.
--
-- XODUS platform: read-only. It reads the table and column list (target setup, deployment check) and counts rows after a
-- promotion; it never writes to the live tables.
-- Data Factory: writes, through its managed identity only. It promotes the validated mirror data into the live tables.

-- XODUS: use the same SQL login the platform uses for the XODUS database (create the login in master first if needed).
IF USER_ID(N'<xodus_sql_login>') IS NULL
    CREATE USER [<xodus_sql_login>] FOR LOGIN [<xodus_sql_login>];
ALTER ROLE db_datareader ADD MEMBER [<xodus_sql_login>];
GO

-- Data Factory managed identity (its name is the factory name, e.g. adf-amp-xodus).
IF USER_ID(N'<adf_factory_name>') IS NULL
    CREATE USER [<adf_factory_name>] FROM EXTERNAL PROVIDER;
ALTER ROLE db_datareader ADD MEMBER [<adf_factory_name>];
ALTER ROLE db_datawriter ADD MEMBER [<adf_factory_name>];
GO

-- Landing schema for promotion: the pipeline copies the validated mirror here, then a procedure in this schema merges it into
-- the live tables in one transaction. Data Factory may create and change objects only in this schema, never in [simplicity].
IF SCHEMA_ID(N'xodus_landing') IS NULL EXEC(N'CREATE SCHEMA [xodus_landing]');
GO
GRANT CREATE TABLE, CREATE PROCEDURE TO [<adf_factory_name>];
GRANT ALTER, EXECUTE ON SCHEMA::[xodus_landing] TO [<adf_factory_name>];
GO
