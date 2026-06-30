import os
import json
import boto3
import logging

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
    """POST /api/v1/tenants/{id}/recommendations/{rec_id}/approve

    Validates that the finding is awaiting approval, then triggers the CodeBuild
    Terraform runner to apply the plan the HCL Writer already staged in main.tf.
    The environment overrides match the contract consumed by buildspec.yml.
    """
    try:
        path_params = event.get('pathParameters') or {}
        tenant_id = path_params.get('id')
        rec_id = path_params.get('rec_id')

        if not tenant_id or not rec_id:
            return _response(400, {"error": "Missing tenant id or rec_id in path parameters."})

        finding = table.get_item(
            Key={'PK': f"TENANT#{tenant_id}", 'SK': f"FINDING#{rec_id}"}
        ).get('Item')

        if not finding:
            return _response(404, {"error": "Recommendation not found."})

        if finding.get('Status') != 'PENDING_APPROVAL':
            return _response(400, {
                "error": f"Cannot approve recommendation in status: {finding.get('Status')}. Must be PENDING_APPROVAL."
            })

        # Tenant execution context (cross-account role + region) lives on the profile item.
        profile = table.get_item(
            Key={'PK': f"TENANT#{tenant_id}", 'SK': "PROFILE"}
        ).get('Item', {})
        tenant_role_arn = profile.get('TenantRoleArn')
        region = profile.get('TargetRegion', 'ap-northeast-1')

        if not tenant_role_arn:
            return _response(409, {"error": "Tenant profile is missing TenantRoleArn; cannot assume execution role."})

        resource_id = finding.get('ResourceId', '')

        build = codebuild.start_build(
            projectName=CODEBUILD_PROJECT,
            environmentVariablesOverride=[
                {'name': 'TENANT_ID', 'value': tenant_id, 'type': 'PLAINTEXT'},
                {'name': 'ACTION_ID', 'value': rec_id, 'type': 'PLAINTEXT'},
                {'name': 'CONFIG_BUCKET', 'value': CONFIG_BUCKET, 'type': 'PLAINTEXT'},
                {'name': 'CONFIG_KEY', 'value': f"{tenant_id}/main.tf", 'type': 'PLAINTEXT'},
                {'name': 'STATE_BUCKET', 'value': STATE_BUCKET, 'type': 'PLAINTEXT'},
                {'name': 'TENANT_ROLE_ARN', 'value': tenant_role_arn, 'type': 'PLAINTEXT'},
                {'name': 'RESOURCE_ID', 'value': resource_id, 'type': 'PLAINTEXT'},
                {'name': 'AWS_REGION', 'value': region, 'type': 'PLAINTEXT'},
            ],
        )

        build_id = build['build']['id']

        table.update_item(
            Key={'PK': f"TENANT#{tenant_id}", 'SK': f"FINDING#{rec_id}"},
            UpdateExpression="SET #status = :status, CodeBuildApplyId = :build_id",
            ExpressionAttributeNames={'#status': 'Status'},
            ExpressionAttributeValues={':status': 'APPLYING', ':build_id': build_id},
        )

        return _response(200, {
            "message": "Approval received. Infrastructure execution initiated.",
            "finding_id": rec_id,
            "build_id": build_id,
            "status": "APPLYING",
        })

    except Exception as e:
        logger.error(f"Approval API Error: {e}", exc_info=True)
        return _response(500, {"error": "Internal Server Error during approval processing."})


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }
