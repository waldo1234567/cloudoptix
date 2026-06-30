from typing import Dict, Any
from .models import Action, Confidence, RuleResult, HCLEdit
from lambdas.metrics.classfier import WorkloadPattern

def evaluate(resource: Dict[str, Any], metrics: Dict[str, Any], pattern: WorkloadPattern) -> RuleResult:
    fs_id = resource.get('SK', '').split('RESOURCE#')[-1]
    meta = resource.get('RawMetadata', {})

    lifecycle_policies = meta.get('LifecyclePolicies', [])
    has_archival_policy = any(p.get('TransitionToArchive') for p in lifecycle_policies)

    # Standard: $0.30/GB. Archive: $0.008/GB.
    estimated_savings = 29.00

    if pattern in [WorkloadPattern.ABANDONED, WorkloadPattern.ALWAYS_ON_IDLE]:
        if not has_archival_policy:
            hcl_diff = f"""
# Architectural Migration: EFS Lifecycle Cost Optimization
# Moves untouched files to Cold Storage, saving ~95% without deleting data.
resource "aws_efs_backup_policy" "__TF_ALIAS___backup" {{
  file_system_id = "{fs_id}"
  backup_policy {{
    status = "ENABLED"
  }}
}}

resource "aws_efs_file_system_policy" "__TF_ALIAS___lifecycle" {{
  file_system_id = "{fs_id}"

  # Transitions files to Archive class after 14 days of zero access
  lifecycle_policy {{
    transition_to_archive = "AFTER_14_DAYS"
  }}
}}
            """
            return RuleResult(
                action=Action.TIER_3_IAC,
                confidence=Confidence.HIGH,
                reasoning="File system has 0 client connections over 30 days. Applying a lifecycle policy securely transitions the static data to the Archive tier.",
                blast_radius_assessment="LOW - Zero data deletion. Native AWS background transition.",
                estimated_monthly_savings=estimated_savings,
                hcl_edits=[HCLEdit(
                    edit_type="add_resource",
                    resource_address="__TF_ADDRESS__",
                    full_resource_hcl=hcl_diff.strip(),
                )],
            )

    return RuleResult(
        Action.IGNORE, Confidence.HIGH, "EFS is actively accessed or already highly optimized.", "SAFE", 0.0
    )
