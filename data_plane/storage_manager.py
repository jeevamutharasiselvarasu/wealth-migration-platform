import hashlib
import os
from pathlib import Path
from typing import Dict, Any, Tuple, Optional

STORAGE_ROOT = Path("App_Data/Storage")


class HybridStorageManager:
    """Manages file persistence across local storage and cloud connectors[cite: 76]."""

    @classmethod
    def save_file(
        cls,
        run_id: str,
        filename: str,
        content: bytes,
        subfolder: str = "RAW"
    ) -> Tuple[str, str, int]:
        """Saves file content to local storage and returns (relative_path, sha256_hash, size_bytes)[cite: 76]."""
        target_dir = STORAGE_ROOT / run_id / subfolder
        target_dir.mkdir(parents=True, exist_ok=True)

        target_path = target_dir / filename
        with open(target_path, "wb") as f:
            f.write(content)

        sha256_hash = hashlib.sha256(content).hexdigest()
        size_bytes = len(content)
        relative_path = f"{run_id}/{subfolder}/{filename}"

        return relative_path, sha256_hash, size_bytes

    @classmethod
    def read_file(cls, relative_path: str) -> bytes:
        """Reads content from storage[cite: 76]."""
        local_path = STORAGE_ROOT / relative_path
        if not local_path.exists():
            raise FileNotFoundError(f"Storage path does not exist: {relative_path}")

        with open(local_path, "rb") as f:
            return f.read()