from typing import Dict, Any
from .models import Action, Confidence, RuleResult
from terraform.platform.src.lambdas.metrics.classfier import WorkloadPattern

def evaluate(resource: Dict[str, Any], metrics: Dict[str, Any], pattern: WorkloadPattern) -> RuleResult:
    meta = resource.get('RawMetadata', {})
    eip_allocation_id = resource.get('SK', '').split('RESOURCE#')[-1]
    
    association_id = meta.get('AssciationId')
    
    if not association_id:
        hcl_diff = f"""
# Architectural Migration: Release unattached Elastic IP
# This IP is not associated with any running instance or NAT Gateway.
- resource "aws_eip" "<your_eip_name>" {{ ... }}
        """
        return RuleResult(
            action=Action.TIER_3_IAC,
            confidence=Confidence.HIGH,
            reasoning="Elastic IP is completely unattached and incurring an hourly AWS penalty fee.",
            blast_radius_assessment="LOW - Resource is isolated and actively wasting money.",
            estimated_monthly_savings=3.60, 
            terraform_hcl_diff=hcl_diff.strip()
        )
    return RuleResult(Action.IGNORE, Confidence.HIGH, "EIP is properly attached to a resource.", "SAFE", 0.0)