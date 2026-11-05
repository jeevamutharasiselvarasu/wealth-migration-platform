"""The Azure environment an ADF package is generated for: the real one when it is configured, else the simulated one.

Real means: the data factory is saved on the Azure connection page and the data lake is a real storage account (not the local
emulator). The storage account comes from the server's blob connection string (account name only; the key is never read here) and
the container from the Azure connection page. SQL and Key Vault are not configured yet, so they stay as clearly marked
placeholders and are listed in ``unresolved``; nothing here signs in to Azure or holds a secret.
"""
import re
from typing import Any, Dict, Optional

from control_plane.services import azure_simulated

PLACEHOLDER = "CHANGE-ME"


def resolve(migration_id: str, azure_cfg: Optional[Dict[str, Any]], storage_account: str, container: str, emulator: bool) -> Dict[str, Any]:
    if not (azure_cfg and azure_cfg.get("factory_name") and azure_cfg.get("subscription_id") and azure_cfg.get("resource_group")
            and storage_account and container and not emulator):
        return azure_simulated.simulated_environment(migration_id)
    slug = re.sub(r"[^a-z0-9]", "", migration_id.lower())[:12] or "xodus"
    sql_ok = bool(azure_cfg.get("sql_server") and azure_cfg.get("sql_database"))
    kv_ok = True  # Azure SQL is reached with the factory's managed identity; Key Vault is not used
    unresolved = [x for x, ok in (("sql", sql_ok), ("keyVault", kv_ok)) if not ok]
    return {
        "simulated": False,
        "banner": ("Generated for the configured Azure environment (data factory and storage). "
                   + ("SQL and Key Vault values are placeholders until they are configured." if unresolved else "")).strip(),
        "subscriptionId": azure_cfg["subscription_id"], "tenantId": azure_cfg.get("tenant_id") or "", "resourceGroup": azure_cfg["resource_group"],
        "factoryName": azure_cfg["factory_name"],
        "storageAccount": storage_account, "storageBlobUrl": f"https://{storage_account}.blob.core.windows.net",
        "storageResourceId": (f"/subscriptions/{azure_cfg['subscription_id']}/resourceGroups/{azure_cfg['resource_group']}"
                              f"/providers/Microsoft.Storage/storageAccounts/{storage_account}"),
        "stagingContainer": container, "stagingFolder": f"stage/{slug}",
        "readyContainer": container, "readyFolder": "curated", "readySuffix": ".ready",
        "sqlServer": azure_cfg.get("sql_server") or f"{PLACEHOLDER}.database.windows.net", "sqlDatabase": azure_cfg.get("sql_database") or PLACEHOLDER,
        "sqlSchema": azure_cfg.get("sql_schema") or "simplicity", "sqlAuth": "managed",
        "keyVaultUrl": azure_cfg.get("key_vault_url") or f"https://{PLACEHOLDER.lower()}.vault.azure.net/",
        "keyVaultSqlSecretName": azure_cfg.get("key_vault_sql_secret") or PLACEHOLDER,
        "unresolved": unresolved,
    }
