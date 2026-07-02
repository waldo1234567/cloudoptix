import os
import json
import logging
import boto3
from datetime import datetime, timezone

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
codebuild = boto3.client('codebuild')

TABLE_NAME = os.environ['DYNAMODB_TABLE_NAME']
CONFIG_BUCKET = os.environ['CONFIG_BUCKET']
STATE_BUCKET = os.environ['STATE_BUCKET']
CODEBUILD_PROJECT = os.environ['CODEBUILD_PROJECT_NAME']
table = dynamodb.Table(TABLE_NAME)


def lambda_handler(event, context):
    """POST /api/v1/tenants/{id}/reconcile

    Runs `terraform apply -refresh-only` via CodeBuild to sync the tenant's state
    with real AWS (e.g. after a resource was deleted in the console). The updated
    state write triggers the state_parser (STATEADDR# reconcile); build_monitor
    then kicks a scan so the RESOURCE# inventory catches up.
    """
    try:
        tenant_id = (event.get('pathParameters') or {}).get('id')
        if not tenant_id:
            return _response(400, {"error": "Missing tenant id in path parameters."})

        profile = table.get_item(
            Key={'PK': f"TENANT#{tenant_id}", 'SK': "PROFILE"}
        ).get('Item')
        if not profile:
            return _response(404, {"error": "Tenant not found."})

        tenant_role_arn = profile.get('TenantRoleArn')
        region = profile.get('TargetRegion', 'ap-northeast-1')
        if not tenant_role_arn:
            return _response(409, {"error": "Tenant profile is missing TenantRoleArn."})
        if not profile.get('WorkspaceReady'):
            return _response(409, {"error": "Workspace not ready — upload your Terraform state first."})

        build = codebuild.start_build(
            projectName=CODEBUILD_PROJECT,
            environmentVariablesOverride=[
                {'name': 'TENANT_ID', 'value': tenant_id, 'type': 'PLAINTEXT'},
                {'name': 'CONFIG_BUCKET', 'value': CONFIG_BUCKET, 'type': 'PLAINTEXT'},
                {'name': 'STATE_BUCKET', 'value': STATE_BUCKET, 'type': 'PLAINTEXT'},
                {'name': 'TENANT_ROLE_ARN', 'value': tenant_role_arn, 'type': 'PLAINTEXT'},
                {'name': 'AWS_REGION', 'value': region, 'type': 'PLAINTEXT'},
                {'name': 'REFRESH_ONLY', 'value': 'true', 'type': 'PLAINTEXT'},
            ],
        )
        build_id = build['build']['id']

        table.update_item(
            Key={'PK': f"TENANT#{tenant_id}", 'SK': "PROFILE"},
            UpdateExpression="SET ReconcileStatus = :s, LastReconcileAt = :t",
            ExpressionAttributeValues={':s': 'RECONCILING', ':t': datetime.now(timezone.utc).isoformat()},
        )

        return _response(202, {"status": "RECONCILING", "build_id": build_id})

    except Exception as e:
        logger.error(f"Reconcile API Error: {e}", exc_info=True)
        return _response(500, {"error": "Internal Server Error."})


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }
