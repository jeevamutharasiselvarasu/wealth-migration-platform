from abc import ABC, abstractmethod
from typing import Dict, List, Any

class BaseTargetAdapter(ABC):
    """Abstract interface that all target system adapters must implement."""

    @abstractmethod
    def transform(self, canonical_records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Converts canonical records into target payload shapes."""
        pass

    @abstractmethod
    def validate_payload(self, target_records: List[Dict[str, Any]]) -> bool:
        """Asserts target schema constraints prior to loading."""
        pass