from typing import Dict, Any
from .models import Action, Confidence, RuleResult, HCLEdit
from lambdas.metrics.classfier import WorkloadPattern

def evaluate(resource: Dict[str, Any], metrics: Dict[str, Any], pattern: WorkloadPattern) -> RuleResult:
    meta = resource.get('RawMetadata', {})
    state = meta.get('State', 'in-use')
    size_gb = meta.get('Size', 0)
    vol_type = meta.get('VolumeType', 'gp2')
    vol_id = resource.get('SK', '').split('RESOURCE#')[-1]

    # Baseline Pricing
    pricing = {
        'gp2': 0.10, 'gp3': 0.08, 'io1': 0.125, 'io2': 0.125, 'sc1': 0.015
    }
    current_rate = pricing.get(vol_type, 0.08)
    monthly_cost = size_gb * current_rate

    if pattern == WorkloadPattern.ABANDONED:
        if state == 'available':
            return RuleResult(
                action=Action.TIER_1_DELETE,
                confidence=Confidence.HIGH,
                reasoning="Volume is completely unattached. A final snapshot is taken before deletion.",
                blast_radius_assessment="NONE - Physically severed. Snapshot preserves data.",
                estimated_monthly_savings=monthly_cost - (size_gb * 0.05),
                hcl_edits=[HCLEdit(
                    edit_type="remove_resource",
                    resource_address="__TF_ADDRESS__",
                )],
                system_tasks=[
                    {
                        "type": "START_BACKUP_JOB",
                        "resource_type": "ebs",
                        "resource_id": vol_id,
                        "vault_name": "CloudOptix-Archive-Vault",
                        "description": f"CloudOptix Backup: Unattached EBS {vol_id}"
                    }
                ],
            )
        else:
            return RuleResult(
                action=Action.TIER_3_IAC,
                confidence=Confidence.HIGH,
                reasoning="Volume is attached but recorded 0 IOPS over 30 days.",
                blast_radius_assessment="MEDIUM - Requires OS unmount before Terraform apply.",
                estimated_monthly_savings=monthly_cost - (size_gb * 0.05),
                hcl_edits=[HCLEdit(
                    edit_type="remove_resource",
                    resource_address="__TF_ADDRESS__",
                )],
                system_tasks=[
                    {
                        "type": "START_BACKUP_JOB",
                        "resource_type": "ebs",
                        "resource_id": vol_id,
                        "vault_name": "CloudOptix-Archive-Vault",
                        "description": f"CloudOptix Backup: Abandoned EBS {vol_id}"
                    }
                ],
            )

    if vol_type == 'gp2':
        return RuleResult(
            action=Action.TIER_3_IAC,
            confidence=Confidence.HIGH,
            reasoning="Legacy gp2 volume detected. Migrating to gp3 saves ~20% with superior baseline performance.",
            blast_radius_assessment="LOW - Zero downtime AWS volume modification.",
            estimated_monthly_savings=size_gb * 0.02,
            hcl_edits=[HCLEdit(
                edit_type="update_attribute",
                resource_address="__TF_ADDRESS__",
                attribute_path="type",
                old_value="gp2",
                new_value="gp3",
            )],
        )

    if vol_type == 'io1':
        return RuleResult(
            action=Action.TIER_3_IAC,
            confidence=Confidence.HIGH,
            reasoning="Legacy io1 volume detected. Migrating to io2 provides 100x higher durability at the same price.",
            blast_radius_assessment="LOW - Zero downtime modification.",
            estimated_monthly_savings=0.0,  # Purely an architectural resilience upgrade
            hcl_edits=[HCLEdit(
                edit_type="update_attribute",
                resource_address="__TF_ADDRESS__",
                attribute_path="type",
                old_value="io1",
                new_value="io2",
            )],
        )

    return RuleResult(Action.IGNORE, Confidence.HIGH, "Volume is active and properly tiered.", "SAFE", 0.0)
