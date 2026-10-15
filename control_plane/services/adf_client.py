import logging
import os
from typing import Any, Dict

logger = logging.getLogger("ADFClient")


class AzureDataFactoryClient:
    """Truthful Azure Data Factory client: never reports a mock or failed call as success."""

    PIPELINE_NAME = "adf_pipe_account_transaction_migration"

    def __init__(self):
        self.subscription_id = os.getenv("AZURE_SUBSCRIPTION_ID")
        self.resource_group = os.getenv("AZURE_RESOURCE_GROUP")
        self.factory_name = os.getenv("ADF_FACTORY_NAME")
        self.mode = os.getenv("ADF_MODE", "azure").strip().lower()

    def _configured(self) -> bool:
        return all((self.subscription_id, self.resource_group, self.factory_name))

    def trigger_entity_pipeline(
        self, migration_id: str, wave_id: str, run_id: str, rep_code: str
    ) -> Dict[str, Any]:
        """Submit a real ADF run, or return an explicitly non-success state."""
        parameters = {
            "migration_id": migration_id,
            "wave_id": wave_id,
            "run_id": run_id,
            "rep_code": rep_code,
        }

        if self.mode == "mock":
            logger.info("ADF mock mode enabled; no Azure request was made.")
            return {
                "status": "MOCKED",
                "adf_run_id": f"ADF-MOCK-{run_id}",
                "pipeline_name": self.PIPELINE_NAME,
                "mode": "MOCK",
                "message": "Simulated response only; no Azure pipeline was submitted.",
            }

        if not self._configured():
            return {
                "status": "NOT_CONFIGURED",
                "adf_run_id": None,
                "pipeline_name": self.PIPELINE_NAME,
                "mode": "UNCONFIGURED",
                "message": "Set AZURE_SUBSCRIPTION_ID, AZURE_RESOURCE_GROUP, and ADF_FACTORY_NAME to submit a real run.",
            }

        try:
            from azure.identity import DefaultAzureCredential
            from azure.mgmt.datafactory import DataFactoryManagementClient

            credential = DefaultAzureCredential()
            client = DataFactoryManagementClient(credential, self.subscription_id)
            response = client.pipelines.create_run(
                self.resource_group,
                self.factory_name,
                self.PIPELINE_NAME,
                parameters=parameters,
            )
            if not getattr(response, "run_id", None):
                return {
                    "status": "FAILED",
                    "adf_run_id": None,
                    "pipeline_name": self.PIPELINE_NAME,
                    "mode": "AZURE_SDK_ACTIVE",
                    "message": "Azure did not return a pipeline run ID.",
                }
            return {
                "status": "SUBMITTED",
                "adf_run_id": response.run_id,
                "pipeline_name": self.PIPELINE_NAME,
                "mode": "AZURE_SDK_ACTIVE",
                "message": "Azure accepted the pipeline submission; execution completion is not yet confirmed.",
            }
        except Exception as exc:
            logger.exception("Azure Data Factory pipeline submission failed.")
            return {
                "status": "FAILED",
                "adf_run_id": None,
                "pipeline_name": self.PIPELINE_NAME,
                "mode": "AZURE_SDK_ACTIVE",
                "message": f"Pipeline submission failed: {type(exc).__name__}: {exc}",
            }

    def get_pipeline_run_status(self, adf_run_id: str) -> Dict[str, Any]:
        """Return the real Azure status or a truthful unavailable/error state."""
        if adf_run_id.startswith("ADF-MOCK-"):
            return {
                "adf_run_id": adf_run_id,
                "status": "MOCKED",
                "message": "This is a simulated run ID; no Azure execution occurred.",
                "duration_ms": None,
            }

        if not self._configured():
            return {
                "adf_run_id": adf_run_id,
                "status": "NOT_CONFIGURED",
                "message": "Azure Data Factory connection settings are not configured.",
                "duration_ms": None,
            }

        try:
            from azure.identity import DefaultAzureCredential
            from azure.mgmt.datafactory import DataFactoryManagementClient

            credential = DefaultAzureCredential()
            client = DataFactoryManagementClient(credential, self.subscription_id)
            run = client.pipeline_runs.get(
                self.resource_group, self.factory_name, adf_run_id
            )
            return {
                "adf_run_id": run.run_id,
                "status": run.status,
                "message": run.message,
                "duration_ms": run.duration_in_ms,
            }
        except Exception as exc:
            logger.exception("Azure Data Factory status lookup failed.")
            return {
                "adf_run_id": adf_run_id,
                "status": "FAILED",
                "message": f"Could not verify Azure run status: {type(exc).__name__}: {exc}",
                "duration_ms": None,
            }
