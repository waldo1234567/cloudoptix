from dataclasses import dataclass, field
from typing import Dict, Any, List, Literal, Optional
from enum import Enum

class Action(Enum):
    TIER_1_STOP   = "TIER_1_STOP"
    TIER_1_DELETE = "TIER_1_DELETE"
    TIER_1_RELEASE = "TIER_1_RELEASE"
    TIER_2_SQUEEZE = "TIER_2_SQUEEZE"
    TIER_3_IAC    = "TIER_3_IAC"
    RECOMMEND     = "RECOMMEND"
    IGNORE        = "IGNORE"

class Confidence(Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


@dataclass
class HCLEdit:
    """
    Structured edit descriptor consumed by the HCL Writer.

    The Writer performs pure text-based pattern matching on the tenant's main.tf
    (no python-hcl2 / AST). Placeholders are resolved by the Writer at apply time:
      __TF_ADDRESS__  -> the real terraform address from the state map (e.g. aws_instance.web)
      __TF_ALIAS__    -> the label segment of that address (e.g. "web"), for naming new resources
    """
    edit_type:         Literal["update_attribute", "remove_resource", "add_resource", "replace_resource"]
    resource_address:  str
    attribute_path:    Optional[str] = None   # for update_attribute, e.g. "instance_type"
    old_value:         Optional[str] = None    # for conflict detection / readability
    new_value:         Optional[str] = None    # new attribute value (update_attribute)
    full_resource_hcl: Optional[str] = None    # full HCL block(s) for add_resource / replace_resource


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
    hcl_edits: Optional[List[HCLEdit]] = None
    system_tasks: Optional[List[Dict[str, Any]]] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Allow rules to pass a single HCLEdit; normalize to a list.
        if isinstance(self.hcl_edits, HCLEdit):
            self.hcl_edits = [self.hcl_edits]
        elif self.hcl_edits is not None and not isinstance(self.hcl_edits, list):
            self.hcl_edits = list(self.hcl_edits)
