import logging
from typing import Dict, Any
from .models import RuleResult, Action, Confidence, HCLEdit
from lambdas.metrics.classfier import WorkloadPattern

logger = logging.getLogger(__name__)


def evaluate(resource: Dict[str, Any], metrics: Dict[str, Any], workload_pattern: str) -> RuleResult:
    meta = resource.get('RawMetadata', {})
    service_name = resource.get('SK', '').split('RESOURCE#')[-1]
    desired_count = meta.get('DesiredCount', 0)

    if desired_count > 0 and workload_pattern == WorkloadPattern.ABANDONED:
        return RuleResult(
            action=Action.TIER_3_IAC,
            confidence=Confidence.HIGH,
            reasoning=f"ECS Service {service_name} is running {desired_count} tasks but receiving no traffic.",
            blast_radius_assessment="SAFE: Tasks are idle. Scaling to 0 pauses billing.",
            estimated_monthly_savings=30.0,
            hcl_edits=[HCLEdit(
                edit_type="update_attribute",
                resource_address="__TF_ADDRESS__",
                attribute_path="desired_count",
                old_value=str(desired_count),
                new_value="0",
            )],
        )

    return RuleResult(action=Action.IGNORE, confidence=Confidence.HIGH, reasoning="Optimized", blast_radius_assessment="N/A", estimated_monthly_savings=0.0)