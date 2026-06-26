import logging
from typing import Dict, Any
from .models import RuleResult, Action, Confidence
from src.lambdas.metrics.classfier import WorkloadPattern

logger = logging.getLogger(__name__)

def evaluate(resource: Any, metrics: Dict[str, Any], workload_pattern: str) -> RuleResult:
    meta = resource.raw_metadata
    service_name = resource.resource_id
    desired_count = meta.get('DesiredCount', 0)
    
    if desired_count > 0 and workload_pattern == WorkloadPattern.ABANDONED:
        hcl = f"""
resource "aws_ecs_service" "{service_name}" {{
  # Reduced desired_count from {desired_count} to 0 due to 0 network/cpu activity
  desired_count = 0
}}
"""
        return RuleResult(
            action=Action.TIER_3_IAC,
            confidence=Confidence.HIGH,
            reasoning=f"ECS Service {service_name} is running {desired_count} tasks but receiving no traffic.",
            blast_radius_assessment="SAFE: Tasks are idle. Scaling to 0 pauses billing.",
            estimated_monthly_savings=30.0,
            terraform_hcl_diff=hcl.strip()
        )
        
    return RuleResult(action=Action.IGNORE, confidence=Confidence.HIGH, reasoning="Optimized", blast_radius_assessment="N/A", estimated_monthly_savings=0.0)