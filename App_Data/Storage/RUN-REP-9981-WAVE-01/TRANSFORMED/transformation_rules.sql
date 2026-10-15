-- ====================================================================
-- XODUS-AMP Data Migration Workbench · Source-to-Canonical SQL Views
-- Generated UTC: 2026-10-14T18:12:02.599716+00:00
-- Engine Target: Azure Data Factory / Azure Synapse / Databricks SQL
-- ====================================================================

-- 1. Canonical Account View
CREATE OR REPLACE VIEW stg_canonical_account AS
SELECT
    TRIM(src.LEGACY_ACCT_NO) AS ACCT_NUM,
    CASE UPPER(TRIM(src.CUST_CODE_SRC))
        WHEN 'SCHW' THEN 'SCHWAB'
        WHEN 'SWB'  THEN 'SCHWAB'
        WHEN 'FID'  THEN 'FIDELITY'
        WHEN 'PER'  THEN 'PERSHING'
        ELSE UPPER(TRIM(src.CUST_CODE_SRC))
    END AS CUSTODIAN_CODE,
    CASE 
        WHEN src.ADVISOR_REP_NUM LIKE 'REP-%' THEN src.ADVISOR_REP_NUM
        ELSE 'REP-' || TRIM(src.ADVISOR_REP_NUM)
    END AS REP_CODE,
    CASE UPPER(TRIM(src.REGISTRATION_SRC_TYPE))
        WHEN 'INDV_TAXABLE' THEN 'INDIVIDUAL'
        WHEN 'TAX'          THEN 'INDIVIDUAL'
        WHEN 'IRA_ROLL'     THEN 'IRA_ROLLOVER'
        ELSE UPPER(TRIM(src.REGISTRATION_SRC_TYPE))
    END AS REG_TYPE,
    TO_DATE(src.ACCOUNT_OPEN_DT, 'MM/DD/YYYY') AS OPEN_DATE
FROM staging_raw.accounts_extract src
WHERE src.LEGACY_ACCT_NO IS NOT NULL;

-- 2. Canonical Sleeve / Allocation View (Rep-as-PM UMA Weights)
CREATE OR REPLACE VIEW stg_canonical_sleeve AS
SELECT
    TRIM(src.SRC_ACCOUNT_NO) AS ACCT_NUM,
    TRIM(src.SUB_SLEEVE_ID) AS SLEEVE_ID,
    CASE UPPER(TRIM(src.STRATEGY_MODEL_ID))
        WHEN 'AMPLIFY-GROWTH-01' THEN 'MOD-GROWTH'
        WHEN 'AMPLIFY-INCOME-02' THEN 'MOD-INCOME'
        ELSE UPPER(TRIM(src.STRATEGY_MODEL_ID))
    END AS MODEL_CODE,
    CAST(src.TARGET_WEIGHT_PCT AS DECIMAL(10,4)) / 100.0000 AS TARGET_WEIGHT,
    CASE 
        WHEN src.REP_CODE_RAW LIKE 'REP-%' THEN src.REP_CODE_RAW
        ELSE 'REP-' || TRIM(src.REP_CODE_RAW)
    END AS REP_CODE
FROM staging_raw.amplify_sleeves_extract src
WHERE src.SUB_SLEEVE_ID IS NOT NULL;

-- 3. Canonical Position (Snapshot) View
CREATE OR REPLACE VIEW stg_canonical_position AS
SELECT
    TRIM(src.ACCT_NUM) AS ACCT_NUM,
    TRIM(src.CUSIP) AS CUSIP,
    TRIM(src.TICKER) AS TICKER,
    CAST(src.QUANTITY AS DECIMAL(18,4)) AS QUANTITY,
    CAST(src.market_value AS DECIMAL(18,2)) AS MARKET_VALUE,
    CURRENT_DATE() AS AS_OF_DATE
FROM staging_raw.amplify_positions_extract src
WHERE src.CUSIP IS NOT NULL;
