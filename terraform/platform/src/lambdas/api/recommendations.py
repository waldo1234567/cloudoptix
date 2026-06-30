import os
import json
import boto3
import logging
from decimal import Decimal
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
TABLE_NAME = os.environ['DYNAMODB_TABLE_NAME']
table = dynamodb.Table(TABLE_NAME)


class DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)


def lambda_handler(event, context):
    """GET /api/v1/tenants/{id}/recommendations[?status=PENDING_APPROVAL]

    Returns the findings produced by the rules engine for a tenant. Each finding
    carries the structured HCLEdit descriptors (TerraformEdits) staged by the
    HCL Writer, plus the resolved terraform address when available.
    """
    try:
        path_params = event.get('pathParameters') or {}
        tenant_id = path_params.get('id')

        query_params = event.get('queryStringParameters') or {}
        status_filter = query_params.get('status')

        if not tenant_id:
            return _response(400, {"error": "Missing tenant id in path parameters."})

        findings = []
        kwargs = {
            'KeyConditionExpression': Key('PK').eq(f"TENANT#{tenant_id}") & Key('SK').begins_with("FINDING#")
        }
        while True:
            resp = table.query(**kwargs)
            findings.extend(resp.get('Items', []))
            if 'LastEvaluatedKey' not in resp:
                break
            kwargs['ExclusiveStartKey'] = resp['LastEvaluatedKey']

        if status_filter:
            findings = [f for f in findings if f.get('Status') == status_filter]

        formatted = []
        for f in findings:
            resource_id = f.get('ResourceId')

            edits = f.get('TerraformEdits')
            if not isinstance(edits, list):
                edits = []

            # Prefer an address already resolved into the edits; otherwise consult the state map.
            resource_address = None
            for edit in edits:
                addr = edit.get('resource_address')
                if addr and addr != '__TF_ADDRESS__':
                    resource_address = addr
                    break

            if not resource_address and resource_id:
                state_map = table.get_item(
                    Key={'PK': f"TENANT#{tenant_id}", 'SK': f"STATEADDR#{resource_id}"}
                ).get('Item')
                if isinstance(state_map, dict):
                    resource_address = state_map.get('TerraformAddress')

            sk_val = f.get('SK')
            sk_str = str(sk_val) if sk_val is not None else ''
            formatted.append({
                "id": sk_str.replace("FINDING#", ""),
                "resource_id": resource_id,
                "resource_type": f.get('ResourceType'),
                "resource_address": resource_address,
                "action": f.get('Action'),
                "status": f.get('Status', 'NEW'),
                "reasoning": f.get('Reasoning'),
                "estimated_monthly_savings": f.get('EstimatedSavings', 0.0),
                "terraform_edits": edits,
                "plan_build_id": f.get('CodeBuildPlanId'),
                "created_at": f.get('CreatedAt'),
            })

        return _response(200, {
            "tenant_id": tenant_id,
            "count": len(formatted),
            "findings": formatted,
        })

    except Exception as e:
        logger.error(f"Recommendations API Error: {e}", exc_info=True)
        return _response(500, {"error": "Internal Server Error."})


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body, cls=DecimalEncoder),
    }
