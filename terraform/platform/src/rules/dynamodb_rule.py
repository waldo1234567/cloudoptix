from typing import Dict, Any
from .models import Action, Confidence, RuleResult, HCLEdit
from lambdas.metrics.classfier import WorkloadPattern

def evaluate(resource: Dict[str, Any], metrics: Dict[str, Any], pattern: WorkloadPattern) -> RuleResult:
    meta = resource.get('RawMetadata', {})
    table_name = resource.get('SK', '').split('RESOURCE#')[-1]

    billing_mode = meta.get('BillingModeSummary', {}).get('BillingMode', 'PROVISIONED')

    # TODO [PROD-BLOCKER]: Replace static $0.00013 estimate with Async SSM Parameter Store cache fetch.
    estimated_monthly_savings = 15.0

    if pattern == WorkloadPattern.ABANDONED:
        if billing_mode == 'PROVISIONED':
            return RuleResult(
                action=Action.TIER_3_IAC,
                confidence=Confidence.HIGH,
                reasoning="Table has zero traffic but is incurring flat hourly charges. Flipping to On-Demand drops the cost to exactly $0/month while preserving data.",
                blast_radius_assessment="LOW - Zero downtime modification. Data is perfectly preserved.",
                estimated_monthly_savings=estimated_monthly_savings,
                hcl_edits=[HCLEdit(
                    edit_type="update_attribute",
                    resource_address="__TF_ADDRESS__",
                    attribute_path="billing_mode",
                    old_value="PROVISIONED",
                    new_value="PAY_PER_REQUEST",
                )],
            )

    if pattern == WorkloadPattern.SPIKY:
        if billing_mode == 'PROVISIONED':
            return RuleResult(
                action=Action.TIER_3_IAC,
                confidence=Confidence.HIGH,
                reasoning="Workload exhibits extreme variance (high peaks, long troughs). On-Demand billing eliminates the waste of provisioned capacity during idle periods.",
                blast_radius_assessment="LOW - Zero downtime modification. AWS handles capacity automatically.",
                estimated_monthly_savings=estimated_monthly_savings,
                hcl_edits=[HCLEdit(
                    edit_type="update_attribute",
                    resource_address="__TF_ADDRESS__",
                    attribute_path="billing_mode",
                    old_value="PROVISIONED",
                    new_value="PAY_PER_REQUEST",
                )],
            )

    if pattern == WorkloadPattern.ALWAYS_ON_ACTIVE:
        if billing_mode == 'PAY_PER_REQUEST':

            read_avg = metrics.get('ConsumedReadCapacityUnits', {}).get('Average', 5)
            write_avg = metrics.get('ConsumedWriteCapacityUnits', {}).get('Average', 5)

            safe_read = int(read_avg * 1.2)
            safe_write = int(write_avg * 1.2)

            return RuleResult(
                action=Action.TIER_3_IAC,
                confidence=Confidence.MEDIUM,
                reasoning="Table has sustained, predictable heavy traffic. Provisioned capacity with Auto Scaling will be significantly cheaper than On-Demand.",
                blast_radius_assessment="MEDIUM - Requires configuring DynamoDB Auto Scaling to accompany the Provisioned limits.",
                estimated_monthly_savings=estimated_monthly_savings,
                hcl_edits=[
                    HCLEdit(
                        edit_type="update_attribute",
                        resource_address="__TF_ADDRESS__",
                        attribute_path="billing_mode",
                        old_value="PAY_PER_REQUEST",
                        new_value="PROVISIONED",
                    ),
                    HCLEdit(
                        edit_type="update_attribute",
                        resource_address="__TF_ADDRESS__",
                        attribute_path="read_capacity",
                        new_value=str(safe_read),
                    ),
                    HCLEdit(
                        edit_type="update_attribute",
                        resource_address="__TF_ADDRESS__",
                        attribute_path="write_capacity",
                        new_value=str(safe_write),
                    ),
                ],
            )

    return RuleResult(
        Action.IGNORE, Confidence.HIGH, f"Table is optimally configured for its workload pattern ({billing_mode}).", "SAFE", 0.0
    )
