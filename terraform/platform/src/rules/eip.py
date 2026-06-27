from typing import Dict, Any
from .models import Action, Confidence, RuleResult
from lambdas.metrics.classfier import WorkloadPattern

def evaluate(resource: Dict[str, Any], metrics: Dict[str, Any], pattern: WorkloadPattern) -> RuleResult:
    meta = resource.get('RawMetadata', {})
    eip_allocation_id = resource.get('SK', '').split('RESOURCE#')[-1]
    
    public_ip = meta.get('PublicIp', eip_allocation_id)
    is_attached = meta.get('IsAttached', True)
    
    if is_attached:
        return RuleResult(
            Action.IGNORE, Confidence.HIGH,
            "EIP is attached to an active resource.",
            "SAFE", 0.0
        )
        
    return RuleResult(
        action=Action.TIER_1_RELEASE,
        confidence=Confidence.HIGH,
        reasoning=f"EIP {public_ip} ({eip_allocation_id}) is unattached and incurring an hourly AWS penalty fee (~$3.60/month).",
        blast_radius_assessment="LOW - Resource is isolated. Release is irreversible; protected by 7-day notification window.",
        estimated_monthly_savings=3.60
    )