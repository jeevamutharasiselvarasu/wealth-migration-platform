-- Creates the Simplicity live database on the Azure SQL logical server. Run while connected to the server's master database
-- (Azure SQL does not support USE, so run 01_live_tables.sql afterwards in a connection to Simplicity itself).
-- The tier below is a small serverless General Purpose database; change SERVICE_OBJECTIVE / MAXSIZE to what you need.
-- Creates only when missing; never drops anything.

IF DB_ID(N'Simplicity') IS NULL
    CREATE DATABASE [Simplicity]
    (
        EDITION = 'GeneralPurpose',
        SERVICE_OBJECTIVE = 'GP_S_Gen5_1',
        MAXSIZE = 32 GB
    );
GO
