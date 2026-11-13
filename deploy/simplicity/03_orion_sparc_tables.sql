-- Simplicity live database, CoreCap (Amplify) to Simplicity spec (docs/source_specs/xodus_migration_spec_corecap_to_simplicity.md, section 3).
-- Run in a connection to the Simplicity database (after 00_create_database.sql), as a database owner or Entra admin. Creates only what is
-- missing; never drops or alters. The target is tables, not flat sheets: schema [orion] (Orion Master Client List, 27 columns in 3 tables)
-- and schema [sparc] (SPARC Household & Account, 34 columns in 5 proposed tables).
--
-- Names and types are placeholders until Simplicity confirms them (the spec gives no lengths: text is nvarchar(255)). Required in the
-- spec = NOT NULL. No primary keys: the promotion merge matches on the key chosen per table in XODUS.
--
-- LINK KEYS (decision D4, open): the flat sheets never needed keys. The columns marked "link key" are a PROPOSAL, nullable, so a load does
-- not depend on the answer. Drop or rename them when Simplicity decides.

IF SCHEMA_ID(N'orion') IS NULL EXEC(N'CREATE SCHEMA [orion]');
GO
IF SCHEMA_ID(N'sparc') IS NULL EXEC(N'CREATE SCHEMA [sparc]');
GO

-- ============================== orion (27 columns) ==============================
IF OBJECT_ID(N'orion.household', N'U') IS NULL
CREATE TABLE [orion].[household] (
    [HH_GROUPING_CODE] nvarchar(255) NULL,       -- HH Grouping Code (required in Orion; source Household_Contacts.HouseholdId; decision D2)
    [ORION_HOUSEHOLD_ID] nvarchar(255) NULL,     -- Orion Household Id (Orion generates it)
    [HOUSEHOLD_SSN] nvarchar(255) NULL,          -- Household SSN (order 1 contact TaxId; blank allowed)
    [HOUSEHOLD_FIRST_NAME] nvarchar(255) NULL,   -- Household First Name
    [HOUSEHOLD_LAST_NAME] nvarchar(255) NOT NULL,-- Household Last Name
    [HOUSEHOLD_NAME] nvarchar(255) NULL,         -- Household Name
    [REPRESENTATIVE_NAME] nvarchar(255) NOT NULL,-- Representative Name (Advisors.Name through Households.AdvisorId)
    [HOUSEHOLD_DOB] date NULL                    -- Household DOB
);
GO
IF OBJECT_ID(N'orion.client', N'U') IS NULL
CREATE TABLE [orion].[client] (
    [HH_GROUPING_CODE] nvarchar(255) NULL,       -- link key (proposal, decision D4): the household of this client
    [ADDRESS_LINE_1] nvarchar(255) NOT NULL,
    [ADDRESS_LINE_2] nvarchar(255) NULL,
    [CITY] nvarchar(255) NOT NULL,
    [STATE] nvarchar(255) NOT NULL,
    [ZIP_CODE] nvarchar(255) NOT NULL,
    [EMAIL_ADDRESS] nvarchar(255) NULL,
    [PHONE_HOME] nvarchar(255) NULL,             -- assumes the source Phone is a home number (decision D9)
    [PHONE_BUSINESS] nvarchar(255) NULL,         -- no source in any sheet
    [REGISTRATION_SSN] nvarchar(255) NULL,
    [REGISTRATION_DOB] date NULL,
    [REGISTRATION_FIRST_NAME] nvarchar(255) NULL,
    [REGISTRATION_LAST_NAME] nvarchar(255) NOT NULL,
    [REGISTRATION_NAME] nvarchar(255) NULL
);
GO
IF OBJECT_ID(N'orion.account', N'U') IS NULL
CREATE TABLE [orion].[account] (
    [HH_GROUPING_CODE] nvarchar(255) NULL,       -- link key (proposal, decision D4): the household of this account
    [REGISTRATION_TYPE] nvarchar(255) NOT NULL,  -- 'Rollover IRA' becomes 'IRA Rollover' (decision D3)
    [CUSTODIAN] nvarchar(255) NOT NULL,
    [ACCOUNT_NUMBER] nvarchar(255) NOT NULL,
    [FUND_NAME] nvarchar(255) NOT NULL,          -- decision D1: the custodian name, or blank
    [OPEN_CLOSED] nvarchar(255) NULL,            -- blank ClosedDate = Open, otherwise Closed
    [OUTSIDE_ID] nvarchar(255) NULL              -- decision D8
);
GO

-- ============================== sparc (34 columns, proposed split) ==============================
IF OBJECT_ID(N'sparc.household', N'U') IS NULL
CREATE TABLE [sparc].[household] (
    [HH_GROUPING_CODE] nvarchar(255) NULL,       -- link key (proposal, decision D4)
    [HOUSEHOLD_NAME] nvarchar(255) NULL,         -- Households.HouseholdProperName
    [BUSINESS_OR_TRUST_NAME] nvarchar(255) NULL, -- only where the contact FirstName contains Trust
    [TRUST_DATE] date NULL,                      -- same condition; the spec takes it from DateOfBirth, which looks wrong
    [BUSINESS_OR_TRUST_EIN] nvarchar(255) NULL   -- same condition
);
GO
IF OBJECT_ID(N'sparc.client', N'U') IS NULL
CREATE TABLE [sparc].[client] (
    [HH_GROUPING_CODE] nvarchar(255) NULL,       -- link key (proposal, decision D4)
    [OWNER_ORDER] int NULL,                      -- 1 = 1st owner / TTEE, 2 = 2nd owner (one row per owner)
    [FIRST_NAME] nvarchar(255) NULL,
    [LAST_NAME] nvarchar(255) NULL,
    [MOBILE_PHONE] nvarchar(255) NULL,
    [EMAIL] nvarchar(255) NULL,
    [STREET_ADDRESS] nvarchar(255) NULL,
    [STREET_ADDRESS_2] nvarchar(255) NULL,
    [CITY] nvarchar(255) NULL,
    [STATE] nvarchar(255) NULL,
    [ZIP_CODE] nvarchar(255) NULL,
    [DOB] date NULL,
    [SSN] nvarchar(255) NULL,
    [EMPLOYMENT] nvarchar(255) NULL
);
GO
IF OBJECT_ID(N'sparc.account', N'U') IS NULL
CREATE TABLE [sparc].[account] (
    [HH_GROUPING_CODE] nvarchar(255) NULL,       -- link key (proposal, decision D4)
    [CUSTODIAN_FUND_FAMILY] nvarchar(255) NULL,
    [ACCOUNT_NUMBER] nvarchar(255) NULL,         -- Account Number (Delivering Firm)
    [REGISTRATION_TYPE] nvarchar(255) NULL,
    [ACCOUNT_VALUE] decimal(19,4) NULL,
    [TOTAL_FEE] decimal(19,4) NULL,              -- Market Value x Account Fee Schedule
    [CONTRIBUTIONS_DISTRIBUTIONS] nvarchar(255) NULL
);
GO
IF OBJECT_ID(N'sparc.account_model', N'U') IS NULL
CREATE TABLE [sparc].[account_model] (
    [ACCOUNT_NUMBER] nvarchar(255) NULL,         -- link key (proposal, decision D4)
    [CURRENT_MODEL] nvarchar(255) NULL,
    [MODEL_SYMBOL] nvarchar(255) NULL
);
GO
IF OBJECT_ID(N'sparc.account_gainloss', N'U') IS NULL
CREATE TABLE [sparc].[account_gainloss] (
    [ACCOUNT_NUMBER] nvarchar(255) NULL,         -- link key (proposal, decision D4)
    [GAIN_LOSS] decimal(19,4) NULL
);
GO
