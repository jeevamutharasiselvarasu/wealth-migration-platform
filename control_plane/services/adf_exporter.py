import os
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


class ADFPackageExporter:
    """Generates executable SQL views and deployable Azure Data Factory (ADF) orchestration bundles."""

    @classmethod
    def generate_sql_views(cls, in_scope_entities: Optional[List[str]] = None) -> str:
        entities = in_scope_entities or ["Account", "Sleeve", "Position"]
        sql_lines = [
            "-- ====================================================================",
            "-- XODUS-AMP Data Migration Workbench · Source-to-Canonical SQL Views",
            f"-- Generated UTC: {datetime.now(timezone.utc).isoformat()}",
            "-- Engine Target: Azure Data Factory / Azure Synapse / Databricks SQL",
            "-- ====================================================================\n"
        ]

        if "Account" in entities or any("Account" in e for e in entities):
            sql_lines.append("""-- 1. Canonical Account View
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
WHERE src.LEGACY_ACCT_NO IS NOT NULL;\n""")

        # Add to generate_sql_views in adf_exporter.py:
        if "Household" in entities or any("Household" in e for e in entities):
            sql_lines.append("""-- Canonical Household View
CREATE OR REPLACE VIEW stg_canonical_household AS
SELECT
    TRIM(src.LEGACY_HH_ID) AS HH_ID,
    TRIM(src.HOUSEHOLD_NAME) AS HH_NAME,
    CASE 
        WHEN src.PRIMARY_REP_NUM LIKE 'REP-%' THEN src.PRIMARY_REP_NUM
        ELSE 'REP-' || TRIM(src.PRIMARY_REP_NUM)
    END AS REP_CODE,
    TO_DATE(src.ESTABLISHED_DT, 'YYYY-MM-DD') AS OPEN_DATE
FROM staging_raw.households_extract src
WHERE src.LEGACY_HH_ID IS NOT NULL;\n""")

        if "Party" in entities or "Client" in entities or any("Party" in e or "Client" in e for e in entities):
            sql_lines.append("""-- Canonical Client / Party View
CREATE OR REPLACE VIEW stg_canonical_party AS
SELECT
    TRIM(src.LEGACY_CLIENT_ID) AS PARTY_ID,
    TRIM(src.FULL_LEGAL_NAME) AS LEGAL_NAME,
    TRIM(src.CLIENT_FIRST_NAME) AS FIRST_NAME,
    TRIM(src.CLIENT_LAST_NAME) AS LAST_NAME,
    CASE UPPER(TRIM(src.CLIENT_TYPE_RAW))
        WHEN 'INDV'  THEN 'INDIVIDUAL'
        WHEN 'TRUST' THEN 'TRUST'
        WHEN 'CORP'  THEN 'CORPORATION'
        WHEN 'LLC'   THEN 'ENTITY'
        ELSE UPPER(TRIM(src.CLIENT_TYPE_RAW))
    END AS PARTY_TYPE,
    TRIM(src.TAX_IDENTIFIER) AS TAX_ID,
    TRIM(src.EMAIL_ADDR) AS EMAIL,
    TRIM(src.PHONE_NUM) AS PHONE,
    TRIM(src.HOUSEHOLD_LINK_ID) AS HH_ID,
    CASE 
        WHEN src.ADVISOR_REP_NUM LIKE 'REP-%' THEN src.ADVISOR_REP_NUM
        ELSE 'REP-' || TRIM(src.ADVISOR_REP_NUM)
    END AS REP_CODE
FROM staging_raw.clients_extract src
WHERE src.LEGACY_CLIENT_ID IS NOT NULL;\n""")

        if "Sleeve" in entities or any("Sleeve" in e for e in entities):
            sql_lines.append("""-- 2. Canonical Sleeve / Allocation View (Rep-as-PM UMA Weights)
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
WHERE src.SUB_SLEEVE_ID IS NOT NULL;\n""")

        if "Position" in entities or any("Position" in e for e in entities):
            sql_lines.append("""-- 3. Canonical Position (Snapshot) View
CREATE OR REPLACE VIEW stg_canonical_position AS
SELECT
    TRIM(src.ACCT_NUM) AS ACCT_NUM,
    TRIM(src.CUSIP) AS CUSIP,
    TRIM(src.TICKER) AS TICKER,
    CAST(src.QUANTITY AS DECIMAL(18,4)) AS QUANTITY,
    CAST(src.market_value AS DECIMAL(18,2)) AS MARKET_VALUE,
    CURRENT_DATE() AS AS_OF_DATE
FROM staging_raw.amplify_positions_extract src
WHERE src.CUSIP IS NOT NULL;\n""")

        return "\n".join(sql_lines)

    @classmethod
    def export_package(
        cls,
        run_id: str,
        migration_id: str = "MIG-CORECAP-2026",
        in_scope_entities: Optional[List[str]] = None,
        transformation_sql: Optional[str] = None,
        validation_plan: Optional[Dict[str, Any]] = None,
        adls_container: str = "reconciled-gold"
    ) -> Dict[str, Any]:
        """Generates package artifacts (transformation_rules.sql, validation_plan.json, adf_pipeline.json, package_manifest.json)."""
        if not transformation_sql:
            transformation_sql = cls.generate_sql_views(in_scope_entities)

        target_path = f"abfss://{adls_container}@xodusamp.dfs.core.windows.net/packages/{run_id}/"
        utc_now = datetime.now(timezone.utc).isoformat()

        if not validation_plan:
            validation_plan = {
                "migration_id": migration_id,
                "run_id": run_id,
                "created_utc": utc_now,
                "sleeve_math_invariant": {
                    "rule": "SUM(target_weight) == Decimal('1.0000')",
                    "tolerance": 0.0005,
                    "action_on_failure": "HOLD_HITL_QUEUE"
                },
                "aum_drift_gate": {
                    "rule": "abs(target_aum - source_aum) / source_aum <= 0.0050",
                    "action_on_failure": "FAIL_HOLD"
                },
                "in_scope_entities": in_scope_entities or ["Account", "Sleeve", "Position"]
            }

        adf_pipeline_json = {
            "$schema": "http://schema.management.azure.com/schemas/2019-06-01/deploymentTemplate.json#",
            "name": f"ADF_Pipe_Migration_{migration_id}",
            "properties": {
                "description": "Orchestrates source-to-canonical staging views and validation gates",
                "parameters": {
                    "RunId": {"type": "string", "defaultValue": run_id},
                    "StorageContainer": {"type": "string", "defaultValue": adls_container}
                },
                "activities": [
                    {
                        "name": "Stage_Canonical_Views",
                        "type": "Script",
                        "typeProperties": {
                            "scripts": [{"text": transformation_sql, "type": "NonQuery"}]
                        }
                    },
                    {
                        "name": "Assert_Sleeve_Invariant_Gate",
                        "type": "Validation",
                        "dependsOn": [{"activity": "Stage_Canonical_Views", "dependencyConditions": ["Succeeded"]}]
                    }
                ]
            }
        }

        adls_linked_service = {
            "name": "LS_AzureDataLakeStorage_Gold",
            "type": "AzureBlobFS",
            "typeProperties": {
                "url": "https://xodusamp.dfs.core.windows.net",
                "accountKey": "ENV_VAR_OR_KEYVAULT"
            }
        }

        manifest = {
            "runId": run_id,
            "migrationId": migration_id,
            "exportedUtc": utc_now,
            "targetLocation": target_path,
            "adfPipelineTarget": f"ADF_Pipe_Migration_{migration_id}",
            "artifacts": {
                "transformation_sql": f"{target_path}transformation_rules.sql",
                "validation_plan": f"{target_path}validation_plan.json",
                "adf_pipeline": f"{target_path}adf_pipeline.json",
                "linked_service_adls": f"{target_path}linkedService_adls.json",
                "package_manifest": f"{target_path}package_manifest.json"
            },
            "status": "PACKAGE_EXPORTED"
        }

        out_dir = Path(f"App_Data/Storage/{run_id}/TRANSFORMED")
        out_dir.mkdir(parents=True, exist_ok=True)
        adf_sub_dir = out_dir / "adf"
        adf_sub_dir.mkdir(parents=True, exist_ok=True)

        with open(out_dir / "transformation_rules.sql", "w", encoding="utf-8") as f:
            f.write(transformation_sql)

        with open(out_dir / "validation_plan.json", "w", encoding="utf-8") as f:
            json.dump(validation_plan, f, indent=2)

        with open(out_dir / "package_manifest.json", "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)

        with open(adf_sub_dir / "adf_pipeline.json", "w", encoding="utf-8") as f:
            json.dump(adf_pipeline_json, f, indent=2)

        with open(adf_sub_dir / "linkedService_adls.json", "w", encoding="utf-8") as f:
            json.dump(adls_linked_service, f, indent=2)

        zip_path = out_dir / f"adf_package_{run_id}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            zipf.write(out_dir / "transformation_rules.sql", arcname="transformation_rules.sql")
            zipf.write(out_dir / "validation_plan.json", arcname="validation_plan.json")
            zipf.write(out_dir / "package_manifest.json", arcname="package_manifest.json")
            zipf.write(adf_sub_dir / "adf_pipeline.json", arcname="adf/adf_pipeline.json")
            zipf.write(adf_sub_dir / "linkedService_adls.json", arcname="adf/linkedService_adls.json")

        manifest["sqlContent"] = transformation_sql
        manifest["zipDownloadPath"] = f"/api/adf/package/download/{run_id}"
        return manifest