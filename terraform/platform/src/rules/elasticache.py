import logging
from typing import Dict, Any
from .models import RuleResult, Action, Confidence, HCLEdit
from lambdas.metrics.classfier import WorkloadPattern

logger = logging.getLogger(__name__)


def evaluate(resource: Dict[str, Any], metrics: Dict[str, Any], workload_pattern: str) -> RuleResult:
    meta = resource.get('RawMetadata', {})
    cluster_id = resource.get('SK', '').split('RESOURCE#')[-1]
    node_type = meta.get('CacheNodeType', 'unknown')

    if workload_pattern == WorkloadPattern.ABANDONED:
        return RuleResult(
            action=Action.TIER_3_IAC,
            confidence=Confidence.HIGH,
            reasoning=f"ElastiCache cluster {cluster_id} has no active connections and is considered abandoned.",
            blast_radius_assessment="LOW: Cache nodes have zero active connections.",
            estimated_monthly_savings=15.0,
            hcl_edits=HCLEdit(
                edit_type="remove_resource",
                resource_address="__TF_ADDRESS__",
                full_resource_hcl=f"# Remove abandoned ElastiCache cluster {cluster_id}",
            ),
            system_tasks=[{"type": "CREATE_ELASTICACHE_SNAPSHOT", "cluster_id": cluster_id}],
        )

    if workload_pattern == WorkloadPattern.ALWAYS_ON_IDLE and not node_type.startswith('cache.t'):
        return RuleResult(
            action=Action.TIER_3_IAC,
            confidence=Confidence.MEDIUM,
            reasoning=f"Cluster {cluster_id} is idle. Downsizing from {node_type} to cache.t3.micro is recommended.",
            blast_radius_assessment="MEDIUM: Cluster will experience a brief downtime during node replacement.",
            estimated_monthly_savings=20.0,
            hcl_edits=HCLEdit(
                edit_type="update_attribute",
                resource_address="__TF_ADDRESS__",
                attribute_path="node_type",
                old_value=node_type,
                new_value="cache.t3.micro",
            ),
        )

    return RuleResult(action=Action.IGNORE, confidence=Confidence.HIGH, reasoning="Optimized", blast_radius_assessment="N/A", estimated_monthly_savings=0.0)