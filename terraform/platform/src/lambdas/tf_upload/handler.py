"""
CloudOptix Path B uploader.

POST /api/v1/tenants/{id}/tf/upload

Stores a tenant's existing Terraform files into their CloudOptix workspace
(s3://${CONFIG_BUCKET}/{tenant_id}/) so the standard writer -> plan -> approve
pipeline can manage them. Their uploaded files are the source of truth; we only
add what is missing (a plain provider when they have none) and our own
CloudOptix-owned backend.tf.

Credential model: the CodeBuild runner assumes the tenant role and injects
temporary credentials into the environment, so providers must NOT carry their
own assume_role (it would double-hop and fail). We therefore never inject
assume_role; a generated provider is plain (region only).
"""
import os
import re
import json
import boto3
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client('s3')
dynamodb = boto3.resource('dynamodb')

TABLE_NAME = os.environ['DYNAMODB_TABLE_NAME']
CONFIG_BUCKET = os.environ['CONFIG_BUCKET']
STATE_BUCKET = os.environ['STATE_BUCKET']
PLATFORM_ACCOUNT_ID = os.environ.get('PLATFORM_ACCOUNT_ID', '')

table = dynamodb.Table(TABLE_NAME)  # type: ignore

# Patterns that indicate the tenant already has a remote backend we must not override.
EXISTING_BACKEND_PATTERNS = [
    re.compile(r'backend\s+"s3"\s*\{'),
    re.compile(r'backend\s+"remote"\s*\{'),
    re.compile(r'backend\s+"cloud"\s*\{'),
    re.compile(r'^\s*cloud\s*\{', re.MULTILINE),  # Terraform Cloud's newer `cloud {}` block
]
PROVIDER_AWS_PATTERN = re.compile(r'provider\s+"aws"\s*\{')


# ── Backend / provider templates (match the buildspec + tenant_mgmt conventions) ──

def _backend_tf(tenant_id: str, region: str) -> str:
    # Matches what buildspec.yml regenerates at apply time (single workspace state,
    # bucket-default encryption, no separate lock table).
    return f"""terraform {{
  backend "s3" {{
    bucket  = "{STATE_BUCKET}"
    key     = "{tenant_id}/terraform.tfstate"
    region  = "{region}"
    encrypt = true
  }}
}}
"""


def _providers_tf(region: str) -> str:
    """Plain provider used only when the tenant uploaded no provider "aws" block.

    No assume_role: the CodeBuild runner injects assumed credentials into the env.
    """
    return f"""terraform {{
  required_providers {{
    aws = {{
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }}
  }}
}}

provider "aws" {{
  region = "{region}"
}}
"""


# ── Detection helpers ──────────────────────────────────────────────────────────

def _has_existing_backend(files: List[Dict[str, str]]) -> bool:
    return any(
        p.search(f.get('content', ''))
        for f in files
        for p in EXISTING_BACKEND_PATTERNS
    )


def _find_provider_aws_file(files: List[Dict[str, str]]) -> Optional[Dict[str, str]]:
    for f in files:
        if PROVIDER_AWS_PATTERN.search(f.get('content', '')):
            return f
    return None


# ── Main handler ───────────────────────────────────────────────────────────────

def _handle_tf_upload(tenant_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    files = body.get('files', [])
    tfstate = body.get('tfstate')

    profile = table.get_item(Key={'PK': f"TENANT#{tenant_id}", 'SK': "PROFILE"}).get('Item')
    if not profile:
        return _error(404, f"Tenant {tenant_id} not found. Register first.")

    region = profile.get('TargetRegion', 'ap-northeast-1')

    if not files:
        return _error(400, "No Terraform files provided.")

    # Guard: do not silently take over an existing remote backend.
    if _has_existing_backend(files):
        return _error(409,
            "Your Terraform configuration already declares a remote backend "
            "(s3 / remote / cloud). CloudOptix cannot safely inject its own backend "
            "without risking state conflicts. Please export your state instead: "
            "run 'terraform state pull > terraform.tfstate' and upload that file "
            "via the tfstate field, removing the backend block from your config."
        )

    provider_file = _find_provider_aws_file(files)

    # Store the tenant's files exactly as uploaded (their config is the source of truth).
    stored_files = []
    for f in files:
        path = (f or {}).get('path')
        content = (f or {}).get('content')
        if not path or content is None:
            return _error(400, "Each file requires 'path' and 'content'.")
        safe_path = path.lstrip('/').replace('..', '')  # guard against prefix escape
        s3.put_object(
            Bucket=CONFIG_BUCKET,
            Key=f"{tenant_id}/{safe_path}",
            Body=str(content).encode('utf-8'),
        )
        stored_files.append(safe_path)

    # No provider "aws" block anywhere — add a plain one.
    if not provider_file:
        s3.put_object(
            Bucket=CONFIG_BUCKET,
            Key=f"{tenant_id}/providers.tf",
            Body=_providers_tf(region).encode('utf-8'),
        )
        stored_files.append("providers.tf")
        logger.info(f"No provider \"aws\" block found — added plain providers.tf for tenant {tenant_id}.")

    # CloudOptix-owned backend (safe: we confirmed no existing backend above).
    s3.put_object(
        Bucket=CONFIG_BUCKET,
        Key=f"{tenant_id}/backend.tf",
        Body=_backend_tf(tenant_id, region).encode('utf-8'),
    )
    stored_files.append("backend.tf")

    # Optional: tenant also provided a tfstate export -> seeds the STATEADDR# map
    # via the state_parser S3 trigger.
    state_status = "not provided"
    if tfstate:
        s3.put_object(
            Bucket=STATE_BUCKET,
            Key=f"{tenant_id}/terraform.tfstate",
            Body=tfstate.encode('utf-8') if isinstance(tfstate, str) else json.dumps(tfstate).encode('utf-8'),
        )
        state_status = "provided — state_parser will populate the STATEADDR# map automatically"

    table.update_item(
        Key={'PK': f"TENANT#{tenant_id}", 'SK': "PROFILE"},
        UpdateExpression="SET OnboardingPath = :p, WorkspaceReady = :r, #status = :s, UpdatedAt = :ts",
        ExpressionAttributeNames={'#status': 'Status'},
        ExpressionAttributeValues={
            ':p': 'B',
            ':r': bool(tfstate),  # address resolution only reliable once we have state
            ':s': 'TF_UPLOADED',
            ':ts': datetime.now(timezone.utc).isoformat(),
        },
    )

    logger.info(f"Path B upload complete for tenant {tenant_id}. Files: {stored_files}")

    return _ok(200, {
        "tenant_id": tenant_id,
        "files_stored": stored_files,
        "tfstate_status": state_status,
        "next_step": (
            "Trigger a scan to begin generating recommendations."
            if tfstate
            else "Run 'terraform state pull > terraform.tfstate' locally and upload it via the "
                 "tfstate field so CloudOptix can resolve resource addresses, or trigger a scan."
        ),
    })


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    try:
        authorizer = event.get('requestContext', {}).get('authorizer', {})
        claims = authorizer.get('jwt', {}).get('claims', {}) or authorizer.get('claims', {})
        caller_id = claims.get('sub') or claims.get('cognito:username')
        if not caller_id:
            return _error(401, "Unauthorized.")
    except Exception as e:
        logger.error(f"Auth error: {e}")
        return _error(500, "Authentication processing failure.")

    path_params = event.get('pathParameters') or {}
    tenant_id = path_params.get('id')
    if not tenant_id:
        return _error(400, "Missing tenant_id in path.")

    try:
        body = json.loads(event.get('body') or '{}')
    except json.JSONDecodeError:
        return _error(400, "Invalid JSON body.")

    return _handle_tf_upload(tenant_id, body)


def _ok(status: int, body: Dict) -> Dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps(body),
    }


def _error(status: int, message: str) -> Dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps({"error": message}),
    }
