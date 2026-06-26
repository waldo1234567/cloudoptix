import logging
import json
from typing import Dict, Any
from .models import RuleResult, Action, Confidence
from src.lambdas.metrics.classfier import WorkloadPattern

logger = logging.getLogger(__name__)

def evaluate(resource: Any, metrics: Dict[str, Any], workload_pattern: str) -> RuleResult:
    meta = resource.raw_metadata
    cluster_id = resource.resource_id
    node_type = meta.get('CacheNodeType', 'unknown')
    
    if workload_pattern == WorkloadPattern.ABANDONED:
        hcl = f"""
# RECOMMENDED ACTION: Remove Abandoned ElastiCache Cluster
# Cluster {cluster_id} has shown 0 connections and near-zero CPU for the observation window.
# resource "aws_elasticache_cluster" "{cluster_id}" {{ ... }} # Remove from state
"""
        return RuleResult(
            action=Action.TIER_3_IAC,
            confidence=Confidence.HIGH,
            reasoning=f"ElastiCache cluster {cluster_id} has no active connections and is considered abandoned.",
            blast_radius_assessment="LOW: Cache nodes have zero active connections.",
            estimated_monthly_savings=15.0, #TODO Placeholder, dynamic calculation goes here
            terraform_hcl_diff=hcl.strip(),
            system_tasks=[{"type": "CREATE_ELASTICACHE_SNAPSHOT", "cluster_id": cluster_id}]
        )
    
    if workload_pattern == WorkloadPattern.ALWAYS_ON_IDLE and not node_type.startswith('cache.t'):
        hcl = f"""
resource "aws_elasticache_cluster" "{cluster_id}" {{
  # Changed from {node_type} to cache.t3.micro due to idle workload
  node_type = "cache.t3.micro"
}}
"""

        return RuleResult(
            action=Action.TIER_3_IAC,
            confidence=Confidence.MEDIUM,
            reasoning=f"Cluster {cluster_id} is idle. Downsizing from {node_type} to cache.t3.micro is recommended.",
            blast_radius_assessment="MEDIUM: Cluster will experience a brief downtime during node replacement.",
            estimated_monthly_savings=20.0,
            terraform_hcl_diff=hcl.strip()
        )
    
    return RuleResult(action=Action.IGNORE, confidence=Confidence.HIGH, reasoning="Optimized", blast_radius_assessment="N/A", estimated_monthly_savings=0.0)