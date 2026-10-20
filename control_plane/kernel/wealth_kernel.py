from typing import Dict, Any


class WealthManagementKnowledgeKernel:
    """Fixed canonical contracts for the XODUS-AMP three-entity pilot.

    The initial pilot deliberately includes only Account, Household and Client.
    Additions to the canonical taxonomy require an explicit platform change.
    """

    CANONICAL_MENU_CARD = {
        "MASTER": [
            {
                "entity_id": "ENT-ACCOUNT",
                "entity_name": "Account",
                "entity_class": "MASTER",
                "grain": "One legal or custodial account identity",
                "expected_file": "accounts.csv",
                "required_fields": ["ACCT_NUM", "CUSTODIAN_CODE", "REP_CODE", "REG_TYPE", "OPEN_DATE"],
                "default_scope": "IN_SCOPE",
            },
            {
                "entity_id": "ENT-HOUSEHOLD",
                "entity_name": "Household",
                "entity_class": "MASTER",
                "grain": "One service or reporting group of related accounts and clients",
                "expected_file": "households.csv",
                "required_fields": ["HH_ID", "HH_NAME", "REP_CODE"],
                "default_scope": "IN_SCOPE",
            },
            {
                "entity_id": "ENT-CLIENT",
                "entity_name": "Client",
                "entity_class": "MASTER",
                "grain": "One individual, trust, or legal client identity",
                "expected_file": "clients.csv",
                "required_fields": ["CLIENT_ID", "LEGAL_NAME", "CLIENT_TYPE"],
                "default_scope": "IN_SCOPE",
            },
        ]
    }

    @classmethod
    def get_canonical_entities(cls) -> Dict[str, Any]:
        """Return the supported canonical entities for the current pilot."""
        return cls.CANONICAL_MENU_CARD
