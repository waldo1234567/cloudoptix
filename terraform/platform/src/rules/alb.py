from typing import Dict, Any
from .models import Action, Confidence, RuleResult, HCLEdit
from lambdas.metrics.classfier import WorkloadPattern

def evaluate(resource: Dict[str, Any], metrics: Dict[str, Any], pattern: WorkloadPattern) -> RuleResult:
    alb_arn = resource.get('SK', '').split('RESOURCE#')[-1]
    alb_name = alb_arn.split('/')[-2] if '/' in alb_arn else alb_arn

    if pattern == WorkloadPattern.ABANDONED:
        return RuleResult(
            action=Action.TIER_3_IAC,
            confidence=Confidence.HIGH,
            reasoning=(
                "ALB received exactly 0 requests over 30 days. Entire ingress infrastructure is orphaned. "
                "Remove the load balancer; its listener and target group blocks should be cleaned up alongside it."
            ),
            blast_radius_assessment="LOW - Graph isolated and mathematically zero traffic.",
            estimated_monthly_savings=16.50,  # Static $0.0225/hr baseline cost
            hcl_edits=[HCLEdit(
                edit_type="remove_resource",
                resource_address="__TF_ADDRESS__",
            )],
        )

    return RuleResult(
        Action.IGNORE, Confidence.HIGH, "ALB is actively processing requests.", "SAFE", 0.0
    )
