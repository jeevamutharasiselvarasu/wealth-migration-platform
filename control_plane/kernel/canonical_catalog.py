from typing import Any, Dict, List

from control_plane.services.firm_config import FirmConfigError, get_firm_config


class CanonicalCatalog:
    """The canonical entities as the firm configuration defines them (nothing about any domain is written here)."""

    @classmethod
    def get_canonical_entities(cls) -> Dict[str, List[Dict[str, Any]]]:
        try:
            cfg = get_firm_config()
        except FirmConfigError:
            return {"MASTER": []}
        return {"MASTER": [{
            "entity_id": "ENT-" + name.upper(),
            "entity_name": name,
            "entity_class": "MASTER",
            "grain": definition.grain,
            "required_fields": cfg.required_fields(name),
            "default_scope": "OUT_OF_SCOPE",  # a migration's scope comes from its setup, never from a default
        } for name, definition in cfg.entities.items()]}
