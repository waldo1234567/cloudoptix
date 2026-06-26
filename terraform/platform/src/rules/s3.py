import logging
from typing import Dict, Any
from .models import RuleResult, Action, Confidence

logger = logging.getLogger(__name__)

def evaluate(resource: Any, metrics: Dict[str, Any], workload_pattern: str) -> RuleResult:
    meta = resource.raw_metadata
    bucket_name = resource.resource_id
    
    size_bytes = meta.get('BucketSizeBytes', 0)
    object_count = meta.get('NumberOfObjects', 0)
    
    if size_bytes == 0 and object_count == 0:
        hcl = f"""
# RECOMMENDED ACTION: Delete Empty Bucket
# resource "aws_s3_bucket" "{bucket_name}" {{ ... }}
"""
        return RuleResult(
            action=Action.TIER_3_IAC,
            confidence=Confidence.HIGH,
            reasoning=f"S3 Bucket {bucket_name} contains 0 objects and 0 bytes.",
            blast_radius_assessment="SAFE: Bucket is completely empty.",
            estimated_monthly_savings=0.0, # Empty buckets don't cost money, but cause clutter
            terraform_hcl_diff=hcl.strip()
        )
        

    has_tiering = meta.get('HasIntelligentTiering', False)
    if size_bytes > (100 * 1024 * 1024 * 1024) and not has_tiering: # > 100GB
        hcl = f"""
resource "aws_s3_bucket_lifecycle_configuration" "{bucket_name}_tiering" {{
  bucket = "{bucket_name}"
  rule {{
    id     = "TransitionToIntelligentTiering"
    status = "Enabled"
    transition {{
      days          = 0
      storage_class = "INTELLIGENT_TIERING"
    }}
  }}
}}
"""
        return RuleResult(
            action=Action.TIER_3_IAC,
            confidence=Confidence.MEDIUM,
            reasoning=f"Bucket {bucket_name} holds >100GB without Intelligent-Tiering.",
            blast_radius_assessment="SAFE: Storage class transition is non-disruptive.",
            estimated_monthly_savings=25.0, # Estimated based on access patterns
            terraform_hcl_diff=hcl.strip()
        )

    return RuleResult(action=Action.NONE, confidence=Confidence.HIGH, reasoning="Optimized", blast_radius_assessment="N/A", estimated_monthly_savings=0.0)