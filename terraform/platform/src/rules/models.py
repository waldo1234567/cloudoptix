from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional
from enum import Enum

class Action(Enum):
    TIER_1_DELETE="TIER_1_DELETE"
    TIER_2_SQUEEZE="TIER_2_SQUEEZE"
    TIER_3_IAC = "TIER_3_IAC"
    RECOMMEND = "RECOMMEND"
    IGNORE = "IGNORE"
    
class Confidence(Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    
@dataclass
class Resource:
    tenant_id: str
    account_id: str
    region: str
    service: str
    resource_type: str
    resource_id: str
    arn: str
    tags: Dict[str,str] = field(default_factory=dict)
    raw_metadata: Dict[str,Any] = field(default_factory=dict)
    
    
    def to_dynamodb_item(self) -> Dict[str, Any]:
        """Formats the resource for the DynamoDB single-table schema."""
        return {
            "PK": f"TENANT#{self.tenant_id}",
            "SK": f"RESOURCE#{self.resource_id}",
            "Type": "ResourceInventory",
            "AccountId": self.account_id,
            "Region": self.region,
            "Service": self.service,
            "ResourceType": self.resource_type,
            "Arn": self.arn,
            "Tags": self.tags,
            "RawMetadata": self.raw_metadata
        }

@dataclass
class RuleResult:
    action: Action
    confidence: Confidence
    reasoning: str
    blast_radius_assessment: str
    estimated_monthly_savings: float
    terraform_hcl_diff: Optional[str] = None
    system_tasks: Optional[List[Dict[str, Any]]] = field(default_factory=list)
    
       
    