from typing import Dict, Any
from .models import Action, Confidence, RuleResult, HCLEdit
from lambdas.metrics.classfier import WorkloadPattern

def evaluate(resource: Dict[str, Any], metrics: Dict[str, Any], pattern: WorkloadPattern) -> RuleResult:
    func_name = resource.get('SK', '').split('RESOURCE#')[-1]

    invocations_sum = metrics.get('Invocations', {}).get('Sum', 1.0)
    errors_sum = metrics.get('Errors', {}).get('Sum', 0.0)
    error_rate = (errors_sum / invocations_sum) * 100 if invocations_sum > 0 else 0.0

    if error_rate > 5.0:
        return RuleResult(
            Action.IGNORE, Confidence.HIGH, f"Function has a high error rate ({error_rate:.1f}%). Requires developer intervention.", "HIGH", 0.0
        )

    if pattern == WorkloadPattern.ABANDONED:
        return RuleResult(
            action=Action.TIER_3_IAC,
            confidence=Confidence.HIGH,
            reasoning=(
                "Function has 0 invocations and 0 throttles over 30 days. Removing it cleans up Terraform state "
                "and eliminates abandoned IAM permissions (remove the associated aws_iam_role block alongside it)."
            ),
            blast_radius_assessment="LOW - Zero active triggers over a 30-day window.",
            estimated_monthly_savings=0.0,  # Zero financial savings, 100% security/hygiene value
            hcl_edits=[HCLEdit(
                edit_type="remove_resource",
                resource_address="__TF_ADDRESS__",
            )],
        )

    return RuleResult(Action.IGNORE, Confidence.HIGH, "Function is actively invoked.", "SAFE", 0.0)
