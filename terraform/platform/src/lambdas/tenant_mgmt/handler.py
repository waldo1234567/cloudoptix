"""
CloudOptix Tenant Management.

Handles tenant onboarding for both architecture paths:

  Path A (Greenfield / console adopters, no existing Terraform):
    Register -> store profile -> scaffold an empty Terraform workspace
    (providers.tf / variables.tf / backend.tf / cloudoptix_managed.tf).
    The first scan later generates imports.tf + main.tf.

  Path B (User already has Terraform / existing IaC):
    Register (has_terraform=true) -> scaffold provider/backend/variables ->
    user uploads their own .tf files via POST .../tf/upload, stored in the
    same S3 workspace.

Note: the tenant must create the cross-account role
'CloudOptix-Tenant-Deployment-Role' (trusting the platform account with the
returned external_id). We record the expected role ARN; we do not deploy into
the tenant account from here.

Routes (HTTP API v2):
  POST /api/v1/tenants/register
  POST /api/v1/tenants/{id}/tf/upload
"""
import os
import json
import uuid
import logging
from datetime import datetime, timezone
from urllib.parse import quote

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client('s3')
dynamodb = boto3.resource('dynamodb')

TABLE_NAME = os.environ['DYNAMODB_TABLE_NAME']
CONFIG_BUCKET = os.environ['CONFIG_BUCKET']
STATE_BUCKET = os.environ['STATE_BUCKET']
PLATFORM_ACCOUNT_ID = os.environ.get('PLATFORM_ACCOUNT_ID', '')
PLATFORM_REGION = os.environ.get('AWS_REGION', 'ap-northeast-1')
CFN_TEMPLATE_URL = os.environ.get('CFN_TEMPLATE_URL', '')
TENANT_ROLE_NAME = "CloudOptix-Tenant-Deployment-Role"


def _launch_stack_url(region: str, external_id: str) -> str:
    """CloudFormation console quick-create URL, pre-filled with the tenant's
    external id + the platform account, so onboarding is one click."""
    if not CFN_TEMPLATE_URL:
        return ""
    return (
        f"https://{region}.console.aws.amazon.com/cloudformation/home?region={region}"
        f"#/stacks/quickcreate?templateURL={quote(CFN_TEMPLATE_URL, safe='')}"
        f"&stackName=CloudOptix-Onboarding"
        f"&param_PlatformAccountID={PLATFORM_ACCOUNT_ID}"
        f"&param_CloudOptixExternalID={external_id}"
    )

table = dynamodb.Table(TABLE_NAME)


def lambda_handler(event, context):
    # tenant_mgmt now handles registration only; Path B file upload lives in the
    # dedicated tf_upload lambda (POST /tenants/{id}/tf/upload).
    route_key = event.get('routeKey', '')
    # Works under both HTTP API payload formats: 2.0 (requestContext.http.method),
    # 1.0 (httpMethod), or falling back to the route key.
    method = (
        event.get('requestContext', {}).get('http', {}).get('method')
        or event.get('httpMethod')
        or _method_from_route(route_key)
    )

    try:
        if method == 'POST':
            return _register_tenant(event)
        return _response(404, {"error": f"Unsupported route: {route_key}"})
    except Exception as e:
        logger.error(f"Tenant management error: {e}", exc_info=True)
        return _response(500, {"error": "Internal Server Error."})


def _register_tenant(event):
    body = _parse_body(event)

    account_id = body.get('account_id')
    region = body.get('region')
    tenant_name = body.get('tenant_name', '')

    if not account_id or not region:
        return _response(400, {"error": "Missing required fields: 'account_id' and 'region'."})

    tenant_id = str(uuid.uuid4())
    external_id = f"cloudoptix-{uuid.uuid4().hex[:16]}"
    tenant_role_arn = f"arn:aws:iam::{account_id}:role/{TENANT_ROLE_NAME}"
    now = datetime.now(timezone.utc).isoformat()

    # Single onboarding path: the tenant brings their own Terraform + state.
    # CloudOptix owns only the backend (injected by the buildspec at plan/apply)
    # and a provider assume_role override added on upload. WorkspaceReady flips
    # true once state is uploaded — nothing can plan/apply before then.
    table.put_item(Item={
        'PK': f"TENANT#{tenant_id}",
        'SK': 'PROFILE',
        'Type': 'TenantProfile',
        'TenantName': tenant_name,
        'TargetAccountId': account_id,
        'TargetRegion': region,
        'TenantRoleArn': tenant_role_arn,
        'ExternalId': external_id,
        'OnboardingPath': 'BYO_TF',
        'WorkspaceReady': False,
        'ScanIntervalHours': 24,
        'ReevaluationIntervalHours': 168,
        'LastScanTime': None,
        'LastReevaluationTime': None,
        'Status': 'ONBOARDING',
        'CreatedAt': now,
    })

    # Scaffold: just the marker. The user's own .tf are the source of truth; the
    # backend is buildspec-owned; the provider override is added by tf_upload.
    workspace_files = _scaffold_workspace(tenant_id)
    for name, content in workspace_files.items():
        s3.put_object(
            Bucket=CONFIG_BUCKET,
            Key=f"{tenant_id}/{name}",
            Body=content.encode('utf-8'),
            ContentType='text/plain',
        )

    return _response(201, {
        "tenant_id": tenant_id,
        "status": "ONBOARDING",
        "cross_account_role": {
            "role_name": TENANT_ROLE_NAME,
            "role_arn": tenant_role_arn,
            "external_id": external_id,
            "trusted_principal": (
                f"arn:aws:iam::{PLATFORM_ACCOUNT_ID}:root" if PLATFORM_ACCOUNT_ID else "CloudOptix platform account"
            ),
            "instructions": (
                "Create this IAM role in your account trusting the platform principal with the given "
                "external_id, then it can be scanned and managed."
            ),
        },
        "cloudformation": {
            "template_url": CFN_TEMPLATE_URL,
            "launch_stack_url": _launch_stack_url(region, external_id),
        },
        "next_step": "Grant cross-account access, then upload your Terraform files and state.",
        "workspace_files": list(workspace_files.keys()),
    })


def _scaffold_workspace(tenant_id):
    marker_tf = f'''# This workspace is managed by CloudOptix.
# Tenant: {tenant_id}
# CloudOptix owns the backend (injected at plan/apply) and adds a provider
# assume_role override on upload. Your own .tf files are the source of truth.
'''
    return {"cloudoptix_managed.tf": marker_tf}


def _method_from_route(route_key: str) -> str:
    return route_key.split(' ', 1)[0] if ' ' in route_key else ''


def _parse_body(event):
    raw = event.get('body') or '{}'
    if event.get('isBase64Encoded'):
        import base64
        raw = base64.b64decode(raw).decode('utf-8')
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }
