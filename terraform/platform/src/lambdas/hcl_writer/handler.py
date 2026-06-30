"""
CloudOptix HCL Writer (text-based).

Consumes findings from the Action Queue, resolves the structured HCLEdit
descriptors against the tenant's main.tf, and applies them using pure
text/regex pattern matching -- NO python-hcl2 / AST. The edit is a "drop-in
and replace on the text": locate the `resource "type" "name" { ... }` block
by regex and surgically rewrite, remove, append, or replace it.

The updated main.tf is written back to the tenant config bucket
(s3://${CONFIG_BUCKET}/${tenant_id}/main.tf) and a CodeBuild *plan-only* run is
triggered (APPLY=false). The finding is moved to PENDING_APPROVAL; the actual
apply happens later via the approve API. The CodeBuild env contract matches
buildspec.yml.

Placeholders emitted by the rules engine are resolved here:
  __TF_ADDRESS__ -> real terraform address from the STATEADDR# map
  __TF_ALIAS__   -> the label segment of that address (for naming new resources)
"""
import os
import re
import json
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger()
logger.setLevel(logging.INFO)

import boto3

s3 = boto3.client('s3')
codebuild = boto3.client('codebuild')
dynamodb = boto3.resource('dynamodb')

CONFIG_BUCKET = os.environ.get('CONFIG_BUCKET', 'cloudoptix-tenant-configs')
STATE_BUCKET = os.environ.get('STATE_BUCKET', '')
TABLE_NAME = os.environ.get('DYNAMODB_TABLE_NAME', 'cloudoptix-core-table')
CODEBUILD_PROJECT = os.environ.get('CODEBUILD_PROJECT_NAME', 'CloudOptix-Terraform-Runner')

table = dynamodb.Table(TABLE_NAME)  # type: ignore


def lambda_handler(event, context):
    logger.info("HCL Writer invoked. Parsing SQS event.")

    for record in event.get('Records', []):
        try:
            body = json.loads(record['body'])
            tenant_id = body.get('tenant_id')
            finding_id = body.get('finding_id')
            resource_id = body.get('resource_id')

            if not all([tenant_id, finding_id, resource_id]):
                # Not a finding message (e.g. a scheduler re-evaluation ping). Ignore.
                logger.info(f"Skipping non-finding message: {body}")
                continue

            logger.info(f"Processing finding {finding_id} for tenant {tenant_id} / resource {resource_id}")

            finding = table.get_item(
                Key={'PK': f"TENANT#{tenant_id}", 'SK': f"FINDING#{finding_id}"}
            ).get('Item')

            if not finding or not finding.get('TerraformEdits'):
                logger.warning(f"Finding {finding_id} has no TerraformEdits. Skipping.")
                continue

            # Resolve the resource's real terraform address from the state map.
            state_map = table.get_item(
                Key={'PK': f"TENANT#{tenant_id}", 'SK': f"STATEADDR#{resource_id}"}
            ).get('Item')
            tf_address = state_map.get('TerraformAddress') if state_map else None

            resolved_edits, skipped = _resolve_edits(finding['TerraformEdits'], resource_id, tf_address)

            if not resolved_edits:
                logger.warning(f"No applicable edits for finding {finding_id} (unmapped resource).")
                _update_finding_status(tenant_id, finding_id, 'SKIPPED_UNMAPPED', skipped=skipped)
                continue

            # Read current main.tf (may not exist yet for a fresh workspace).
            config_key = f"{tenant_id}/main.tf"
            try:
                obj = s3.get_object(Bucket=CONFIG_BUCKET, Key=config_key)
                tf_content = obj['Body'].read().decode('utf-8')
            except s3.exceptions.NoSuchKey:
                logger.warning(f"main.tf not found for {tenant_id}; initializing empty.")
                tf_content = ""

            new_tf_content = _apply_text_surgery(tf_content, resolved_edits)

            # Safety: never blank out a non-empty source.
            if not new_tf_content.strip() and tf_content.strip():
                logger.error("Parser returned empty content despite non-empty input. Aborting S3 write.")
                continue

            s3.put_object(
                Bucket=CONFIG_BUCKET,
                Key=config_key,
                Body=new_tf_content.encode('utf-8'),
            )
            logger.info(f"Updated main.tf for {tenant_id}. Triggering terraform plan (preview only).")

            # Tenant execution context for the CodeBuild runner.
            profile = table.get_item(
                Key={'PK': f"TENANT#{tenant_id}", 'SK': "PROFILE"}
            ).get('Item', {})
            tenant_role_arn = profile.get('TenantRoleArn', '')
            region = profile.get('TargetRegion', 'ap-northeast-1')

            build = codebuild.start_build(
                projectName=CODEBUILD_PROJECT,
                environmentVariablesOverride=[
                    {'name': 'TENANT_ID', 'value': tenant_id, 'type': 'PLAINTEXT'},
                    {'name': 'ACTION_ID', 'value': finding_id, 'type': 'PLAINTEXT'},
                    {'name': 'CONFIG_BUCKET', 'value': CONFIG_BUCKET, 'type': 'PLAINTEXT'},
                    {'name': 'STATE_BUCKET', 'value': STATE_BUCKET, 'type': 'PLAINTEXT'},
                    {'name': 'TENANT_ROLE_ARN', 'value': tenant_role_arn, 'type': 'PLAINTEXT'},
                    {'name': 'RESOURCE_ID', 'value': resource_id, 'type': 'PLAINTEXT'},
                    {'name': 'AWS_REGION', 'value': region, 'type': 'PLAINTEXT'},
                    {'name': 'APPLY', 'value': 'false', 'type': 'PLAINTEXT'},
                ],
            )

            _update_finding_status(
                tenant_id, finding_id, 'PENDING_APPROVAL',
                build_id=build.get('build', {}).get('id', ''),
                skipped=skipped,
            )
            logger.info(f"Staged plan for finding {finding_id}.")

        except Exception as e:
            logger.error(f"HCL Writer error on record: {e}", exc_info=True)
            raise

    return {"statusCode": 200, "body": "Processed."}


def _resolve_edits(edits: List[Dict[str, Any]], resource_id: str, tf_address: Optional[str]):
    """Substitutes __TF_ADDRESS__ / __TF_ALIAS__ placeholders with real values."""
    resolved = []
    skipped = []
    tf_alias = tf_address.split('.')[-1] if tf_address else None

    for edit in edits:
        edit = dict(edit)  # don't mutate the DynamoDB item in place
        needs_address = edit.get('edit_type') != 'add_resource'

        if edit.get('resource_address') == '__TF_ADDRESS__':
            if not tf_address:
                if needs_address:
                    skipped.append({
                        "resource_id": resource_id,
                        "reason": ("No Terraform state mapping found. Resource may have been created via the "
                                   "AWS console. Import it first: "
                                   f"terraform import <resource_type>.<label> {resource_id}"),
                    })
                    continue
            else:
                edit['resource_address'] = tf_address

        if edit.get('full_resource_hcl') and tf_alias:
            edit['full_resource_hcl'] = edit['full_resource_hcl'].replace('__TF_ALIAS__', tf_alias)

        resolved.append(edit)

    return resolved, skipped


def _apply_text_surgery(tf_content: str, edits: List[Dict[str, Any]]) -> str:
    """Applies each edit to the raw HCL text via regex pattern matching."""
    modified = tf_content

    for edit in edits:
        edit_type = edit.get('edit_type')

        # add_resource: pure append, no block lookup required.
        if edit_type == 'add_resource':
            raw_hcl = (edit.get('full_resource_hcl') or '').strip()
            if raw_hcl:
                modified += f"\n\n{raw_hcl}\n"
            continue

        res_address = edit.get('resource_address')
        if not res_address or len(res_address.split('.')) != 2:
            logger.error(f"Invalid resource address: {res_address}")
            continue

        res_type, res_name = res_address.split('.')
        pattern = re.compile(rf'resource\s+"{re.escape(res_type)}"\s+"{re.escape(res_name)}"\s*{{')
        match = pattern.search(modified)

        if not match:
            logger.warning(f"Resource {res_address} not found in main.tf. Skipping {edit_type}.")
            continue

        block_start_idx = match.start()
        open_brace_idx = match.end() - 1

        # Walk braces to find the matching close for this resource block.
        brace_count = 0
        close_brace_idx = -1
        for i in range(open_brace_idx, len(modified)):
            if modified[i] == '{':
                brace_count += 1
            elif modified[i] == '}':
                brace_count -= 1
                if brace_count == 0:
                    close_brace_idx = i
                    break

        if close_brace_idx == -1:
            logger.error(f"Malformed HCL: no closing brace for {res_address}.")
            continue

        if edit_type == 'remove_resource':
            modified = modified[:block_start_idx] + modified[close_brace_idx + 1:]

        elif edit_type == 'replace_resource':
            raw_hcl = (edit.get('full_resource_hcl') or '').strip()
            if raw_hcl:
                modified = modified[:block_start_idx] + raw_hcl + "\n" + modified[close_brace_idx + 1:]

        elif edit_type == 'update_attribute':
            attribute = edit.get('attribute_path')
            new_value = edit.get('new_value')
            if not attribute:
                logger.error(f"update_attribute missing attribute_path for {res_address}.")
                continue

            block = modified[block_start_idx:close_brace_idx + 1]
            attr_pattern = re.compile(rf'({re.escape(attribute)}\s*=\s*)(".*?"|\'.*?\'|[^\s#\n]+)')
            formatted = _format_value(new_value)

            if attr_pattern.search(block):
                new_block = attr_pattern.sub(rf'\g<1>{formatted}', block, count=1)
            else:
                # Attribute absent: insert it just before the closing brace.
                new_block = block[:-1] + f"  {attribute} = {formatted}\n}}"

            modified = modified[:block_start_idx] + new_block + modified[close_brace_idx + 1:]

    return modified


def _format_value(new_value: Any) -> str:
    """Quotes plain string values; leaves references / expressions / maps untouched."""
    s = str(new_value)
    if s.startswith(('${', 'aws_', '{', '[')):
        return s
    return f'"{s}"'


def _update_finding_status(tenant_id, finding_id, status, build_id=None, skipped=None):
    expr = "SET #status = :status"
    names = {'#status': 'Status'}
    values: Dict[str, Any] = {':status': status}

    if build_id is not None:
        expr += ", CodeBuildPlanId = :build_id"
        values[':build_id'] = build_id
    if skipped:
        expr += ", SkippedResources = :skipped"
        values[':skipped'] = skipped

    table.update_item(
        Key={'PK': f"TENANT#{tenant_id}", 'SK': f"FINDING#{finding_id}"},
        UpdateExpression=expr,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )
