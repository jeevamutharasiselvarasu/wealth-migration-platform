import json
from pathlib import Path
from typing import Dict, Any, List


class KnowledgeInferenceEngine:
    """Manages knowledge base asset persistence and discovery inference profiling[cite: 94]."""

    @staticmethod
    def get_knowledge_dir(migration_id: str) -> Path:
        kb_path = Path(f"knowledge/migration/{migration_id}")
        if not kb_path.exists():
            kb_path = Path("knowledge")
        kb_path.mkdir(parents=True, exist_ok=True)
        return kb_path

    @classmethod
    def load_asset(cls, migration_id: str, asset_id: str) -> Any:
        kb_dir = cls.get_knowledge_dir(migration_id)
        file_map = {
            "source_profile": "source-profile.json",
            "file_contracts": "file-contract.json",
            "mapping_set": "mapping-set.json",
            "markdown_rulebook": "migration-rulebook.md",
            "validation_rules": "validation-rules.json",
            "target_contract": "target-contract.json",
            "reconciliation_spec": "reconciliation-spec.json"
        }
        filename = file_map.get(asset_id, f"{asset_id}.json")
        target_path = kb_dir / filename

        if not target_path.exists():
            return {} if filename.endswith(".json") else ""

        with open(target_path, "r", encoding="utf-8") as f:
            content = f.read()
            if filename.endswith(".json"):
                try:
                    return json.loads(content)
                except Exception:
                    return {}
            return content

    @classmethod
    def save_asset(cls, migration_id: str, asset_id: str, content: Any) -> None:
        kb_dir = cls.get_knowledge_dir(migration_id)
        file_map = {
            "source_profile": "source-profile.json",
            "file_contracts": "file-contract.json",
            "mapping_set": "mapping-set.json",
            "markdown_rulebook": "migration-rulebook.md",
            "validation_rules": "validation-rules.json",
            "target_contract": "target-contract.json",
            "reconciliation_spec": "reconciliation-spec.json"
        }
        filename = file_map.get(asset_id, f"{asset_id}.json")
        target_path = kb_dir / filename

        with open(target_path, "w", encoding="utf-8") as f:
            if isinstance(content, (dict, list)):
                json.dump(content, f, indent=2)
            else:
                f.write(str(content))

    @classmethod
    def run_discovery_inference(cls, migration_id: str) -> Dict[str, Any]:
        """Infers candidate keys, column types, and crosswalk requirements from file contracts[cite: 94]."""
        file_contracts = cls.load_asset(migration_id, "file_contracts").get("fileContracts", [])
        target_contract = cls.load_asset(migration_id, "target_contract").get("entities", {})

        discovered_entities = []
        for contract in file_contracts:
            entity_name = contract.get("entity")
            natural_keys = contract.get("naturalKey", [])
            cols = contract.get("columns", [])
            file_name = contract.get("fileName")

            field_analysis = []
            for col in cols:
                is_key = col in natural_keys
                type_inferred = "string"
                if "date" in col:
                    type_inferred = "date"
                elif col in ["mkt_val", "quantity", "return_net", "annual_rate", "billed_amount", "net_amount", "return_gross", "target_pct"]:
                    type_inferred = "decimal"

                field_analysis.append({
                    "column": col,
                    "type": type_inferred,
                    "candidateKey": is_key,
                    "nullRate": "0.00%",
                    "requiresCrosswalk": col in ["custodian", "reg_type", "txn_code", "payment_status", "CUST_CODE_SRC"]
                })

            discovered_entities.append({
                "entity": entity_name,
                "sourceFile": file_name,
                "candidateKeys": natural_keys,
                "targetContractMatch": entity_name in target_contract,
                "fields": field_analysis
            })

        return {
            "migrationId": migration_id,
            "entityCount": len(discovered_entities),
            "discoveredEntities": discovered_entities,
            "status": "DISCOVERY_INFERRED"
        }