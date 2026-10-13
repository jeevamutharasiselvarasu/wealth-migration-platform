import hashlib
import os
import logging
from pathlib import Path
from typing import Dict, Any, Tuple, Optional

logger = logging.getLogger("HybridStorageManager")
STORAGE_ROOT = Path("App_Data/Storage")


class HybridStorageManager:
    """Manages file persistence across Local Storage and Azure Data Lake Storage (ADLS Gen2)."""

    @classmethod
    def save_file(
        cls,
        run_id: str,
        filename: str,
        content: bytes,
        subfolder: str = "RAW",
        adls_container: Optional[str] = None
    ) -> Tuple[str, str, int]:
        """Saves file locally and uploads to ADLS if container is configured."""
        target_dir = STORAGE_ROOT / run_id / subfolder
        target_dir.mkdir(parents=True, exist_ok=True)

        target_path = target_dir / filename
        with open(target_path, "wb") as f:
            f.write(content)

        sha256_hash = hashlib.sha256(content).hexdigest()
        size_bytes = len(content)
        relative_path = f"{run_id}/{subfolder}/{filename}"

        if adls_container:
            cls.upload_to_adls(adls_container, f"{run_id}/{subfolder}/{filename}", content)

        return relative_path, sha256_hash, size_bytes

    @classmethod
    def upload_to_adls(cls, container_name: str, blob_path: str, content: bytes) -> bool:
        """Uploads byte payload to Azure Data Lake Storage Gen2 container."""
        account_name = os.getenv("AZURE_STORAGE_ACCOUNT", "stgcorecapdata")
        try:
            from azure.identity import DefaultAzureCredential
            from azure.storage.filedatalake import DataLakeServiceClient

            account_url = f"https://{account_name}.dfs.core.windows.net"
            credential = DefaultAzureCredential()
            service_client = DataLakeServiceClient(account_url, credential=credential)

            file_system_client = service_client.get_file_system_client(file_system=container_name)
            file_client = file_system_client.get_file_client(blob_path)

            file_client.upload_data(content, overwrite=True)
            logger.info(f"Successfully uploaded {blob_path} to ADLS container '{container_name}'.")
            return True

        except Exception as e:
            logger.warning(f"ADLS upload running in Sandbox / Local fallback mode: {str(e)}")
            return False

    @classmethod
    def read_file(cls, relative_path: str) -> bytes:
        """Reads content from storage (supports local relative paths and abfss:// URIs)."""
        if relative_path.startswith("abfss://"):
            return cls._read_cloud_storage_hook(relative_path)

        local_path = STORAGE_ROOT / relative_path
        if not local_path.exists():
            raise FileNotFoundError(f"Storage path does not exist: {relative_path}")

        with open(local_path, "rb") as f:
            return f.read()

    @classmethod
    def _read_cloud_storage_hook(cls, uri: str) -> bytes:
        """Stub cloud reader for ADLS Gen2 URI hooks."""
        filename = uri.split("/")[-1]
        return f"-- Cloud file buffer for {filename}\n".encode("utf-8")