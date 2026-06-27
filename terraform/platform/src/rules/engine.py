import json
import os
import logging
from typing import Dict, Any, List
from .models import Action, Confidence, RuleResult
from boto3.dynamodb.conditions import Key
from lambdas.metrics.classfier import classify_resource,WorkloadPattern
from . import ec2, rds, ebs, alb, natgateway, dynamodb_rule, lambda_rules, eip, efs, elasticache, ecs, s3
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ.get('DYNAMODB_TABLE_NAME', 'cloudoptix-core-table'))
sqs = boto3.client('sqs')
ACTION_QUEUE_URL = os.environ.get('ACTION_QUEUE_URL')

MODULE_MAP = {
    'instance': ec2,
    'volume': ebs,
    'db-instance': rds,
    'function': lambda_rules,
    'loadbalancer': alb,
    'natgateway': natgateway,
    'table': dynamodb_rule,
    'cluster': elasticache,
    'service': ecs,       
    'bucket': s3,         
    'eipalloc': eip,      
    'filesystem': efs
}

def lambda_handler(event, context):
    for record in event.get('Records', []):
        body = json.loads(record['body'])
        tenant_id = body['tenant_id']
        
        logger.info(f"Rules Engine Started for Tenant: {tenant_id}")
        
        profile = table.get_item(Key={'PK': f"TENANT#{tenant_id}", 'SK': "PROFILE"}).get('Item', {})
        tenant_role_arn = profile.get('TenantRoleArn')
        region = profile.get('TargetRegion', 'ap-northeast-1')
        sns_topic_arn = os.environ.get('SNS_TOPIC_ARN')
        
        resources = _get_tenant_resources(tenant_id)
        
        for resource_data in resources:
            res_id = resource_data.get('SK', '').split('RESOURCE#')[-1]
            res_type = resource_data.get('ResourceType')
            
            rule_result = evaluate_resource(resource_data)
            
            if rule_result.action not in [Action.IGNORE]:
                logger.info(f"Action {rule_result.action} required for {res_id}. Pushing to Action Queue.")
                
                execution_payload = {
                    "tenant_id": tenant_id,
                    "tenant_role_arn": tenant_role_arn,
                    "region": region,
                    "sns_topic_arn": sns_topic_arn,
                    "resource_id": res_id,
                    "resource_type": res_type,
                    "rule_result": {
                        "action": rule_result.action.value,
                        "system_tasks": getattr(rule_result, 'system_tasks', []),
                        "validation_endpoint": getattr(rule_result, 'validation_endpoint', None),
                        "terraform_hcl_diff": getattr(rule_result, 'terraform_hcl_diff', None),
                        "reasoning": getattr(rule_result, 'reasoning', None),
                        "estimated_monthly_savings": getattr(rule_result, 'estimated_monthly_savings', 0.0)
                    }
                }
                
                sqs.send_message(
                    QueueUrl=ACTION_QUEUE_URL,
                    MessageBody=json.dumps(execution_payload),
                    MessageGroupId=f"{tenant_id}-{res_id}"
                )
    
    return {"Status" : "Success"}
                

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
    rule_module = MODULE_MAP.get(res_type) # type: ignore
    if not rule_module:
        return RuleResult(
            action=Action.IGNORE, 
            confidence=Confidence.LOW, 
            reasoning=f"No V2 rules module mapped for {res_type}", 
            blast_radius_assessment="N/A", 
            estimated_monthly_savings=0.0
        )
    
    try:
        workload_pattern = classify_resource(resource_data)
        
        return rule_module.evaluate(resource_data, metrics, workload_pattern)
    
    except Exception as e:
        logger.error(f"Error evaluating rules for {res_id}: {e}")
        return RuleResult(
            action=Action.IGNORE, 
            confidence=Confidence.LOW, 
            reasoning=f"Rule evaluation failed: {str(e)}", 
            blast_radius_assessment="UNKNOWN", 
            estimated_monthly_savings=0.0
        )

def _get_tenant_resources(tenant_id: str) -> List[Dict[str, Any]]:
    items = []
    kwargs = {
        'KeyConditionExpression': Key('PK').eq(f"TENANT#{tenant_id}") & Key('SK').begins_with("RESOURCE#")
    }
    
    while True:
        response = table.query(**kwargs)
        items.extend(response.get('Items', []))
        if 'LastEvaluatedKey' not in response: break
        kwargs['ExclusiveStartKey'] = response['LastEvaluatedKey']
    return items