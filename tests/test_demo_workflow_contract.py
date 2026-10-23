import json
from pathlib import Path

from control_plane.services.entity_classification_agent import classify_source_file
from control_plane.services.source_file_adapter import read_source_records


ROOT = Path(__file__).resolve().parents[1]


def test_demo_manifest_files_are_all_parseable_and_classifiable():
    manifest = json.loads((ROOT / "demo_data" / "demo_migration.json").read_text(encoding="utf-8"))
    source_files = manifest["source_files"]

    assert len(source_files) == 9
    assert set(manifest["migration_setup"]["entities"]) == {"Account", "Client", "Household"}

    for item in source_files:
        path = (ROOT / item["path"]).resolve()
        assert path.is_relative_to(ROOT.resolve())
        records = read_source_records(path.name, path.read_bytes())
        assert records, f"{path.name} should include sample rows"
        result = classify_source_file(
            list(records[0].keys()),
            records=records,
            context=" ".join(
                part for part in (item.get("context", ""), item.get("dictionary_notes", ""))
                if part
            ),
        )
        assert result["status"] in {
            "INFERRED", "NEEDS_REVIEW", "NEEDS_CLARIFICATION", "RELATIONSHIP_REVIEW"
        }, path.name
        assert result["approval_status"] == "PENDING"
        assert result["classification_type"] in {"MASTER_ENTITY", "RELATIONSHIP", "UNKNOWN"}


def test_demo_knowledge_references_resolve_to_registered_inputs():
    manifest = json.loads((ROOT / "demo_data" / "demo_migration.json").read_text(encoding="utf-8"))
    setup = manifest["migration_setup"]
    knowledge_items = manifest.get("knowledge_files") or [
        {"path": setup.get("knowledge_schema_ref", "")},
        {"path": setup.get("knowledge_rules_ref", "")},
    ]

    assert len(knowledge_items) == 2
    for item in knowledge_items:
        path = (ROOT / item["path"]).resolve()
        assert path.is_relative_to(ROOT.resolve())
        assert path.is_file(), f"Knowledge reference is missing: {item['path']}"
        assert path.read_bytes().strip()
