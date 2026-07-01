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
TENANT_ROLE_NAME = "CloudOptix-Tenant-Deployment-Role"

table = dynamodb.Table(TABLE_NAME)


def lambda_handler(event, context):
    # tenant_mgmt now handles registration only; Path B file upload lives in the
    # dedicated tf_upload lambda (POST /tenants/{id}/tf/upload).
    route_key = event.get('routeKey', '')
    method = event.get('requestContext', {}).get('http', {}).get('method') or _method_from_route(route_key)

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
    has_terraform = bool(body.get('has_terraform', False))
    tenant_name = body.get('tenant_name', '')

    if not account_id or not region:
        return _response(400, {"error": "Missing required fields: 'account_id' and 'region'."})

    tenant_id = str(uuid.uuid4())
    external_id = f"cloudoptix-{uuid.uuid4().hex[:16]}"
    tenant_role_arn = f"arn:aws:iam::{account_id}:role/{TENANT_ROLE_NAME}"
    onboarding_path = "B" if has_terraform else "A"
    now = datetime.now(timezone.utc).isoformat()

    # 1. Persist the tenant profile (schema consumed by scheduler/scanner/rules).
    table.put_item(Item={
        'PK': f"TENANT#{tenant_id}",
        'SK': 'PROFILE',
        'Type': 'TenantProfile',
        'TenantName': tenant_name,
        'TargetAccountId': account_id,
        'TargetRegion': region,
        'TenantRoleArn': tenant_role_arn,
        'ExternalId': external_id,
        'OnboardingPath': onboarding_path,
        'HasTerraform': has_terraform,
        'ScanIntervalHours': 24,
        'ReevaluationIntervalHours': 168,
        'LastScanTime': None,
        'LastReevaluationTime': None,
        'Status': 'ONBOARDING',
        'CreatedAt': now,
    })

    # 2. Scaffold the S3 Terraform workspace.
    workspace_files = _scaffold_workspace(tenant_id, account_id, region, tenant_role_arn, external_id, onboarding_path)
    for name, content in workspace_files.items():
        s3.put_object(
            Bucket=CONFIG_BUCKET,
            Key=f"{tenant_id}/{name}",
            Body=content.encode('utf-8'),
            ContentType='text/plain',
        )

    return _response(201, {
        "tenant_id": tenant_id,
        "onboarding_path": onboarding_path,
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
        "next_step": (
            "Upload your Terraform files via POST /api/v1/tenants/{id}/tf/upload."
            if onboarding_path == "B"
            else "Trigger the first scan to auto-generate Terraform imports for your existing resources."
        ),
        "workspace_files": list(workspace_files.keys()),
    })


def _scaffold_workspace(tenant_id, account_id, region, tenant_role_arn, external_id, onboarding_path):
    # Cross-account model: the CodeBuild backend runs as the platform role, and
    # the provider assumes the tenant role for resource operations.
    providers_tf = f'''provider "aws" {{
  region = var.region

  assume_role {{
    role_arn    = "{tenant_role_arn}"
    external_id = "{external_id}"
  }}
}}
'''

    variables_tf = f'''variable "region" {{
  type    = string
  default = "{region}"
}}

variable "account_id" {{
  type    = string
  default = "{account_id}"
}}
'''

    backend_tf = f'''terraform {{
  backend "s3" {{
    bucket = "{STATE_BUCKET}"
    key    = "{tenant_id}/terraform.tfstate"
    region = "{PLATFORM_REGION}"
  }}
}}
'''

    marker_tf = f'''# This workspace is managed by CloudOptix.
# Onboarding path: {onboarding_path}
# Tenant: {tenant_id}
# Do not edit generated files by hand; changes are reconciled on each scan.
'''

    files = {
        "variables.tf": variables_tf,
        "backend.tf": backend_tf,
        "cloudoptix_managed.tf": marker_tf,
    }

    if onboarding_path == "A":
        # Greenfield: own the provider and seed an empty main.tf the first scan fills.
        files["providers.tf"] = providers_tf
        files["main.tf"] = "# CloudOptix-managed resources will be generated here on first scan.\n"
    # Path B: provider + main.tf come from the tenant via tf_upload, so we do not
    # scaffold providers.tf here (it would collide with their provider block).

    return files


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
