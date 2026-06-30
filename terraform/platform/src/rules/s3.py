import logging
from typing import Dict, Any
from .models import RuleResult, Action, Confidence, HCLEdit
from lambdas.metrics.classfier import WorkloadPattern

logger = logging.getLogger(__name__)


def evaluate(resource: Dict[str, Any], metrics: Dict[str, Any], workload_pattern: str) -> RuleResult:
    meta = resource.get('RawMetadata', {})
    bucket_name = resource.get('SK', '').split('RESOURCE#')[-1]

    size_bytes = meta.get('BucketSizeBytes', 0)
    object_count = meta.get('NumberOfObjects', 0)

    if size_bytes == 0 and object_count == 0:
        return RuleResult(
            action=Action.TIER_3_IAC,
            confidence=Confidence.HIGH,
            reasoning=f"S3 Bucket {bucket_name} contains 0 objects and 0 bytes.",
            blast_radius_assessment="SAFE: Bucket is completely empty.",
            estimated_monthly_savings=0.0,
            hcl_edits=[HCLEdit(
                edit_type="remove_resource",
                resource_address="__TF_ADDRESS__",
                full_resource_hcl=f"# Remove empty bucket {bucket_name}",
            )]
        )

    has_tiering = meta.get('HasIntelligentTiering', False)
    if size_bytes > (100 * 1024 * 1024 * 1024) and not has_tiering:  # > 100GB
        return RuleResult(
            action=Action.TIER_3_IAC,
            confidence=Confidence.MEDIUM,
            reasoning=f"Bucket {bucket_name} holds >100GB without Intelligent-Tiering.",
            blast_radius_assessment="SAFE: Storage class transition is non-disruptive.",
            estimated_monthly_savings=25.0,
            hcl_edits=[HCLEdit(
                edit_type="add_resource",
                resource_address="__TF_ADDRESS__",
                full_resource_hcl=(
                    f'resource "aws_s3_bucket_lifecycle_configuration" "__TF_ALIAS___tiering" {{\n'
                    f'  bucket = "aws_s3_bucket.__TF_ALIAS__.id"\n'
                    '  rule {\n'
                    '    id     = "TransitionToIntelligentTiering"\n'
                    '    status = "Enabled"\n'
                    '    transition {\n'
                    '      days          = 0\n'
                    '      storage_class = "INTELLIGENT_TIERING"\n'
                    '    }\n'
                    '  }\n'
                    '}\n'
                ),
            )]
        )

    return RuleResult(action=Action.IGNORE, confidence=Confidence.HIGH, reasoning="Optimized", blast_radius_assessment="N/A", estimated_monthly_savings=0.0)