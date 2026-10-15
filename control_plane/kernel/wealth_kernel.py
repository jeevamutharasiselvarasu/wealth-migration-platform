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
                "required_fields": ["ACCT_NUM", "CUSTODIAN_CODE", "REP_CODE", "REG_TYPE", "OPEN_DATE"],
                "default_scope": "IN_SCOPE"
            },
            {
                "entity_id": "ENT-HOUSEHOLD",
                "entity_name": "Household",
                "entity_class": "MASTER",
                "grain": "One business service/reporting grouping of related accounts",
                "expected_file": "households.csv",
                "required_fields": ["HH_ID", "HH_NAME", "REP_CODE"],
                "default_scope": "IN_SCOPE"
            },
            {
                "entity_id": "ENT-CLIENT",
                "entity_name": "Client",
                "entity_class": "MASTER",
                "grain": "One person, trust, or legal participant in identity domain",
                "expected_file": "clients.csv",
                "required_fields": ["CLIENT_ID", "LEGAL_NAME", "TAX_ID", "CLIENT_TYPE"],
                "default_scope": "IN_SCOPE"
            },
            {
                "entity_id": "ENT-ADVISOR",
                "entity_name": "Advisor / Rep",
                "entity_class": "MASTER",
                "grain": "One financial representative or rep code role",
                "expected_file": "advisors.csv",
                "required_fields": ["REP_CODE", "ADVISOR_NAME", "CRD_NUMBER", "FIRM_ID"],
                "default_scope": "IN_SCOPE"
            },
            {
                "entity_id": "ENT-MODEL",
                "entity_name": "Model",
                "entity_class": "MASTER",
                "grain": "One reusable investment strategy or Rep-as-PM strategy definition",
                "expected_file": "models.csv",
                "required_fields": ["MODEL_CODE", "MODEL_NAME", "STRATEGY_TYPE", "IS_REP_PM"],
                "default_scope": "IN_SCOPE"
            },
            {
                "entity_id": "ENT-SECURITY",
                "entity_name": "Security Master",
                "entity_class": "MASTER",
                "grain": "One investable reference security instrument",
                "expected_file": "securities.csv",
                "required_fields": ["CUSIP", "TICKER", "SECURITY_NAME", "ASSET_CLASS"],
                "default_scope": "IN_SCOPE"
            }
        ],
        "RELATIONSHIP": [
            {
                "entity_id": "ENT-SLEEVE",
                "entity_name": "Sleeve / Allocation",
                "entity_class": "RELATIONSHIP",
                "grain": "Applied model component with target allocation weight",
                "expected_file": "sleeves.csv",
                "required_fields": ["ACCT_NUM", "SLEEVE_ID", "MODEL_CODE", "TARGET_PCT"],
                "default_scope": "IN_SCOPE"
            },
            {
                "entity_id": "ENT-ACCOUNT-OWNER",
                "entity_name": "Account Owner",
                "entity_class": "RELATIONSHIP",
                "grain": "Client ownership/authority association on custodial account",
                "expected_file": "account_owners.csv",
                "required_fields": ["ACCT_NUM", "CLIENT_ID", "OWNERSHIP_ROLE_CODE"],
                "default_scope": "IN_SCOPE"
            }
        ],
        "EVENT_AND_SNAPSHOT": [
            {
                "entity_id": "ENT-POSITION",
                "entity_name": "Position (Snapshot)",
                "entity_class": "SNAPSHOT",
                "grain": "One asset holding snapshot for an account/sleeve at an as-of date",
                "expected_file": "positions.csv",
                "required_fields": ["ACCT_NUM", "CUSIP", "QUANTITY", "MARKET_VALUE", "AS_OF_DATE"],
                "default_scope": "IN_SCOPE"
            },
            {
                "entity_id": "ENT-TRANSACTION",
                "entity_name": "Transaction",
                "entity_class": "EVENT",
                "grain": "One posted financial or ledger event",
                "expected_file": "transactions.csv",
                "required_fields": ["TRANS_ID", "ACCT_NUM", "TRANS_CODE", "AMOUNT", "TRADE_DATE"],
                "default_scope": "IN_SCOPE"
            },
            {
                "entity_id": "ENT-PERFORMANCE",
                "entity_name": "Performance Period",
                "entity_class": "SNAPSHOT",
                "grain": "One measured period return and balance record",
                "expected_file": "performance.csv",
                "required_fields": ["ACCT_NUM", "PERIOD", "BMV", "EMV", "NET_RETURN"],
                "default_scope": "IN_SCOPE"
            },
            {
                "entity_id": "ENT-TAX-LOT",
                "entity_name": "Tax Lot",
                "entity_class": "SNAPSHOT",
                "grain": "One acquisition lot component of a position",
                "expected_file": "tax_lots.csv",
                "required_fields": ["LOT_ID", "ACCT_NUM", "CUSIP", "COST_BASIS", "ACQ_DATE"],
                "default_scope": "PHASE_2"
            }
        ],
        "ARCHIVAL": [
            {
                "entity_id": "ENT-DOCUMENTS",
                "entity_name": "Documents & Statements",
                "entity_class": "CONTROL",
                "grain": "Historical statements, suitability PDFs, and compliance evidence",
                "expected_file": "documents_manifest.csv",
                "required_fields": ["DOC_ID", "ACCT_NUM", "DOC_TYPE", "AWS_S3_URI"],
                "default_scope": "ARCHIVE_ONLY"
            },
            {
                "entity_id": "ENT-TERMINATED",
                "entity_name": "Terminated Accounts",
                "entity_class": "CONTROL",
                "grain": "Closed accounts requiring regulatory WORM retention only",
                "expected_file": "terminated_accounts.csv",
                "required_fields": ["ACCT_NUM", "CLOSE_DATE", "ARCHIVE_REASON"],
                "default_scope": "ARCHIVE_ONLY"
            }
        ]
    }

    @classmethod
    def get_canonical_entities(cls) -> Dict[str, Any]:
        return cls.CANONICAL_MENU_CARD