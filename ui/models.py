from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any


class CreateMigrationRequest(BaseModel):
    name: str = Field(..., description="Unique migration name")
    category: str = "Custom migration"
    description: Optional[str] = None
    sourceSystem: str = "Source System"
    targetSystem: str = "Target System"
    entities: List[str] = []
    templateId: Optional[str] = None
    createdBy: str = "migration-analyst"


class RulebookUpdateRequest(BaseModel):
    content: str
    updatedBy: str = "migration-analyst"


class CreateRunRequest(BaseModel):
    sourceSystemId: Optional[str] = None
    targetSystemId: Optional[str] = None
    migrationId: str
    createdBy: str = "migration-analyst"
    canonicalModelVersion: str = "wealth-v1"


class ApprovalRequest(BaseModel):
    artifactId: Optional[str] = None
    stage: str
    decision: str  # APPROVED or REJECTED
    reviewer: str  # Migration Analyst or Validation Specialist
    comments: Optional[str] = None


class RuleRerunRequest(BaseModel):
    entity: str
    targetField: Optional[str] = None
    reviewer: str = "migration-analyst"