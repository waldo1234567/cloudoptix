from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
import uuid
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Dict, Any, List
import uuid
from .models import Action, Confidence, RuleResult
from boto3.dynamodb.conditions import Key
from lambdas.metrics.classfier import classify_resource, WorkloadPattern
from . import ec2, rds, ebs, alb, natgateway, dynamodb_rule, lambda_rules, eip, efs, elasticache, ecs, s3
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ.get('DYNAMODB_TABLE_NAME', 'cloudoptix-core-table'))  # type: ignore
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
    'eip': eip,
    'filesystem': efs,
}


def lambda_handler(event, context):
    for record in event.get('Records', []):
        body = json.loads(record['body'])
        tenant_id = body['tenant_id']

        logger.info(f"Rules Engine Started for Tenant: {tenant_id}")

        resources = _get_tenant_resources(tenant_id)

        for resource_data in resources:
            res_id = resource_data.get('SK', '').split('RESOURCE#')[-1]
            res_type = resource_data.get('ResourceType')

            rule_result = evaluate_resource(resource_data)

            if rule_result.action == Action.IGNORE:
                continue

            finding_id = f"f-{uuid.uuid4().hex[:8]}"
            logger.info(f"Action {rule_result.action.value} required for {res_id}. Persisting finding {finding_id}.")

            # Serialize the structured edit descriptors for the text-based HCL Writer.
            terraform_edits = [asdict(edit) for edit in (rule_result.hcl_edits or [])]

            _publish_finding_to_db(
                tenant_id=tenant_id,
                resource_id=res_id,
                resource_type=res_type,
                finding_id=finding_id,
                rule_result=rule_result,
                terraform_edits=terraform_edits,
            )

            # The Writer re-reads the finding (incl. TerraformEdits) from DynamoDB by id.
            sqs.send_message(
                QueueUrl=ACTION_QUEUE_URL,
                MessageBody=json.dumps({
                    "tenant_id": tenant_id,
                    "finding_id": finding_id,
                    "resource_id": res_id,
                }),
                MessageGroupId=f"{tenant_id}-{res_id}",
            )

    return {"Status": "Success"}


def evaluate_resource(resource_data: Dict[str, Any]) -> RuleResult:
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
    rule_module = MODULE_MAP.get(res_type)  # type: ignore
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
        if 'LastEvaluatedKey' not in response:
            break
        kwargs['ExclusiveStartKey'] = response['LastEvaluatedKey']
    return items


def _publish_finding_to_db(tenant_id, resource_id, resource_type, finding_id, rule_result: RuleResult, terraform_edits):
    table.put_item(Item={
        'PK': f"TENANT#{tenant_id}",
        'SK': f"FINDING#{finding_id}",
        'ResourceId': resource_id,
        'ResourceType': resource_type,
        'Status': 'NEW',
        'Action': rule_result.action.value,
        'Reasoning': rule_result.reasoning or 'Optimization recommended.',
        'EstimatedSavings': str(rule_result.estimated_monthly_savings or '0.00'),
        'TerraformEdits': terraform_edits,
        'SystemTasks': rule_result.system_tasks or [],
        'CreatedAt': datetime.now(timezone.utc).isoformat(),
    })
    return finding_id
