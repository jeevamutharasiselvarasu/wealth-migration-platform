-- Simplicity live database: the Orion MCL target tables XODUS promotes validated data into.
-- Run against the Simplicity database (after 00_create_database.sql). Creates only what is missing; never drops or alters.
-- Columns, types and required flags follow the Orion MCL import spec (docs/source_specs/Data_Mapping_MCLSPARC.xlsx, as
-- captured in simulated_target_service/seed.yaml). The spec gives no lengths, so text columns are nvarchar(255): adjust
-- before the first load if Orion needs other lengths. Required in the spec = NOT NULL here.
-- No primary keys: the spec defines none. The promotion merge matches on the key chosen per table in XODUS.

IF SCHEMA_ID(N'simplicity') IS NULL EXEC(N'CREATE SCHEMA [simplicity]');
GO

-- Orion MasterClientListWizard import: Household grouping and household-level names
IF OBJECT_ID(N'simplicity.ORION_MCL_HOUSEHOLD', N'U') IS NULL
CREATE TABLE [simplicity].[ORION_MCL_HOUSEHOLD] (
    [HH_GROUPING_CODE] nvarchar(255) NULL,  -- HH Grouping Code
    [ORION_HOUSEHOLD_ID] nvarchar(255) NULL,  -- Orion Household Id
    [HOUSEHOLD_SSN] nvarchar(255) NULL,  -- Household SSN
    [HOUSEHOLD_FIRST_NAME] nvarchar(255) NULL,  -- Household First Name
    [HOUSEHOLD_LAST_NAME] nvarchar(255) NOT NULL,  -- Household Last Name
    [HOUSEHOLD_NAME] nvarchar(255) NULL,  -- Household Name
    [REPRESENTATIVE_NAME] nvarchar(255) NOT NULL,  -- Representative Name
    [HOUSEHOLD_DOB] date NULL  -- Household DOB
);
GO

-- Orion MasterClientListWizard import: Account owners (clients), their contact details and identifiers
IF OBJECT_ID(N'simplicity.ORION_MCL_CLIENT', N'U') IS NULL
CREATE TABLE [simplicity].[ORION_MCL_CLIENT] (
    [ADDRESS_LINE_1] nvarchar(255) NOT NULL,  -- Address Line 1
    [ADDRESS_LINE_2] nvarchar(255) NULL,  -- Address Line 2
    [CITY] nvarchar(255) NOT NULL,  -- City
    [STATE] nvarchar(255) NOT NULL,  -- State
    [ZIP_CODE] nvarchar(255) NOT NULL,  -- Zip Code
    [EMAIL_ADDRESS] nvarchar(255) NULL,  -- Email Address
    [PHONE_HOME] nvarchar(255) NULL,  -- Phone Home
    [PHONE_BUSINESS] nvarchar(255) NULL,  -- Phone Business
    [REGISTRATION_SSN] nvarchar(255) NULL,  -- Registration SSN
    [REGISTRATION_DOB] date NULL,  -- Registration DOB
    [REGISTRATION_FIRST_NAME] nvarchar(255) NULL,  -- Registration First Name
    [REGISTRATION_LAST_NAME] nvarchar(255) NOT NULL,  -- Registration Last Name
    [REGISTRATION_NAME] nvarchar(255) NULL  -- Registration Name
);
GO

-- Orion MasterClientListWizard import: Custodial accounts
IF OBJECT_ID(N'simplicity.ORION_MCL_ACCOUNT', N'U') IS NULL
CREATE TABLE [simplicity].[ORION_MCL_ACCOUNT] (
    [REGISTRATION_TYPE] nvarchar(255) NOT NULL,  -- Registration Type
    [CUSTODIAN] nvarchar(255) NOT NULL,  -- Custodian
    [ACCOUNT_NUMBER] nvarchar(255) NOT NULL,  -- Account Number
    [FUND_NAME] nvarchar(255) NOT NULL,  -- Fund Name
    [OPEN_CLOSED] nvarchar(255) NULL,  -- Open/Closed
    [OUTSIDE_ID] nvarchar(255) NULL  -- Outside ID
);
GO
