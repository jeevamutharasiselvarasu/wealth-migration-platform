from typing import Dict, Any, List

class WealthManagementKnowledgeKernel:
    """Fixed platform canonical entity taxonomy and invariants (Decoupled from user firm profiles)."""

    # Inbuilt Platform Entities (Common to all wealth acquisition migrations)
    CANONICAL_MENU_CARD = {
        "MASTER": [
            {
                "entity_id": "ENT-ACCOUNT",
                "entity_name": "Account",
                "entity_class": "MASTER",
                "grain": "One legal/custodial account identity",
                "expected_file": "accounts.csv",
                "expected_files": ["accounts.csv", "account_name_address.csv", "account_type.csv"],
                "source_roles": ["ACCOUNT_MASTER", "NAME_ADDRESS", "ACCOUNT_TYPE"],
                "required_fields": ["ACCT_NUM", "CUSTODIAN_CODE", "REP_CODE", "REG_TYPE", "OPEN_DATE"],
                "default_scope": "IN_SCOPE"
            },
            {
                "entity_id": "ENT-CLIENT",
                "entity_name": "Client",
                "entity_class": "MASTER",
                "grain": "One person, trust, or legal participant in the identity domain",
                "expected_file": "clients.csv",
                "expected_files": ["clients.csv", "client_model.csv"],
                "source_roles": ["ENTITY_MASTER", "CLIENT_MODEL"],
                "required_fields": ["CLIENT_ID", "LEGAL_NAME", "TAX_ID", "CLIENT_TYPE"],
                "default_scope": "IN_SCOPE"
            },
            {
                "entity_id": "ENT-HOUSEHOLD",
                "entity_name": "Household",
                "entity_class": "MASTER",
                "grain": "One business/reporting grouping of related clients and/or accounts",
                "expected_file": "households.csv",
                "expected_files": ["households.csv", "household_mapping.csv"],
                "source_roles": ["ENTITY_MASTER", "HOUSEHOLD_MAPPING"],
                "required_fields": ["HH_ID", "HH_NAME", "REP_CODE"],
                "default_scope": "IN_SCOPE"
            }
        ]
    }

    @classmethod
    def get_canonical_entities(cls) -> Dict[str, Any]:
        return cls.CANONICAL_MENU_CARD