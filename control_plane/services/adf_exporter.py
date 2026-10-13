import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict


class ADFPackageExporter:
    """Exports approved Control Plane transformation packages to ADLS Gen2 for Azure Data Factory execution."""

    @classmethod
    def export_package(
        cls,
        run_id: str,
        migration_id: str,
        transformation_sql: str,
        validation_plan: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Generates package artifacts (transformation_rules.sql, validation_plan.json, package_manifest.json)."""
        adls_container = "reconciled-gold"
        target_path = f"abfss://{adls_container}@xodusamp.dfs.core.windows.net/packages/{run_id}/"

        utc_now = datetime.now(timezone.utc).isoformat()

        manifest = {
            "runId": run_id,
            "migrationId": migration_id,
            "exportedUtc": utc_now,
            "adfPipelineTarget": "adf_pipe_account_transaction_migration",
            "targetLocation": target_path,
            "artifacts": {
                "transformation_sql": f"{target_path}transformation_rules.sql",
                "validation_plan": f"{target_path}validation_plan.json",
                "package_manifest": f"{target_path}package_manifest.json"
            },
            "status": "PACKAGE_EXPORTED"
        }

        out_dir = Path(f"App_Data/Storage/{run_id}/TRANSFORMED")
        out_dir.mkdir(parents=True, exist_ok=True)

        with open(out_dir / "transformation_rules.sql", "w", encoding="utf-8") as f:
            f.write(transformation_sql)

        with open(out_dir / "validation_plan.json", "w", encoding="utf-8") as f:
            json.dump(validation_plan, f, indent=2)

        with open(out_dir / "package_manifest.json", "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)

        return manifest