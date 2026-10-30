"""Simulated Azure environment values for ADF package generation.

Every value here is fictitious and deterministic. No Azure subscription, factory, storage account or SQL
server exists behind them, no secret is held, and nothing is ever deployed. Secrets are represented only as
Key Vault secret references. Replace this module's output with the real environment values when live Azure
integration is explicitly in scope.
"""
from typing import Any, Dict

SIMULATED_BANNER = "SIMULATED AZURE VALUES - not a real environment. Do not deploy without replacing every value."


def simulated_environment(migration_id: str) -> Dict[str, Any]:
    slug = "".join(ch.lower() for ch in migration_id if ch.isalnum())[:12] or "xodus"
    return {
        "simulated": True,
        "banner": SIMULATED_BANNER,
        "subscriptionId": "00000000-0000-0000-0000-000000000000",
        "tenantId": "11111111-1111-1111-1111-111111111111",
        "resourceGroup": "rg-xodus-sim",
        "location": "eastus",
        "factoryName": "adf-xodus-sim",
        "storageAccount": "stxodussim",
        "storageDfsUrl": "https://stxodussim.dfs.core.windows.net",
        "stagingContainer": "canonical-staging",
        "stagingFolder": f"{slug}",
        "sqlServer": "sql-xodus-sim.database.windows.net",
        "sqlDatabase": "simplicity_target_sim",
        "sqlSchema": "dbo",
        "keyVaultUrl": "https://kv-xodus-sim.vault.azure.net/",
        "keyVaultSqlSecretName": "simplicity-sql-connection-string",
    }
