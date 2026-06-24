import importlib
import logging
from typing import Dict, Any
from .models import Action, Confidence, RuleResult
from terraform.platform.src.lambdas.metrics.classfier import classify_resource,WorkloadPattern

logger = logging.getLogger()

def evaluate_resource(resource_data: Dict[str,Any]) -> RuleResult:
    """
    Enforces global safety gates, classifies the workload pattern, and routes to specific generators.
    """
    res_id = resource_data.get('SK', '').split('RESOURCE#')[-1]
    res_type = resource_data.get('ResourceType')
    
    is_unsafe = resource_data.get('IsUnsafe', True)
    metrics = resource_data.get('MetricSnapshot', {})
    
    if is_unsafe:
        return RuleResult(
            action=Action.IGNORE,
            confidence=Confidence.HIGH,
            reasoning="Resource is reachable from a production anchor via dependency graph.",
            blast_radius_assessment="HIGH - Modifying this resource cascades to production.",
            estimated_monthly_savings=0.0
        )
        
    if not metrics:
        return RuleResult(
            action=Action.IGNORE,
            confidence=Confidence.HIGH,
            reasoning="No CloudWatch metric snapshot available. Cannot mathematically prove state.",
            blast_radius_assessment="UNKNOWN - Missing telemetry.",
            estimated_monthly_savings=0.0
        )
        
    
    workload_pattern = classify_resource(resource_data)
    
    if workload_pattern == WorkloadPattern.ALWAYS_ON_ACTIVE and res_type not in ['volume', 'db-instance', 'table']:
        return RuleResult(
            action=Action.IGNORE,
            confidence=Confidence.HIGH,
            reasoning="Resource is actively utilized and fits its current configuration model.",
            blast_radius_assessment="SAFE",
            estimated_monthly_savings=0.0
        )
        
    try:
        module_map = {
            'instance': 'ec2',
            'volume': 'ebs',
            'db-instance': 'rds',
            'function': 'lambda_rules',
            'loadbalancer': 'alb',
            'natgateway': 'nat_gateway',
            'table': 'dynamodb',
            'cluster': 'elasticache'
        }
        
        
        rule_module_name = module_map.get(res_type) # type: ignore
        if not rule_module_name:
            return RuleResult(
                action=Action.IGNORE, 
                confidence=Confidence.LOW, 
                reasoning=f"No V2 rules module mapped for {res_type}", 
                blast_radius_assessment="N/A", 
                estimated_monthly_savings=0.0
            )
            
        rule_module = importlib.import_module(f".{rule_module_name}", package="rules")
        return rule_module.evaluate(resource_data, metrics, workload_pattern)
    
    except Exception as e:
        logger.error(f"Error evaluating rules for {res_id}: {e}")
        return RuleResult(
            action=Action.IGNORE, 
            confidence=Confidence.LOW, 
            reasoning=f"Engine exception: {str(e)}", 
            blast_radius_assessment="ERROR", 
            estimated_monthly_savings=0.0
        )