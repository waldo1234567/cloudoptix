"""
CloudOptix Build Monitor.

Triggered by EventBridge on CodeBuild "Build State Change" (terminal states) for
the Terraform runner. Reads the finished build's overrides (TENANT_ID, ACTION_ID
= finding id, APPLY) via batch_get_builds, then reconciles the finding's status
so a plan/apply outcome is actually reflected instead of the writer optimistically
marking everything PENDING_APPROVAL.

Status transitions:
  plan  (APPLY=false)  SUCCEEDED -> PENDING_APPROVAL   FAILED -> PLAN_FAILED
  apply (APPLY=true)   SUCCEEDED -> APPLIED             FAILED -> APPLY_FAILED

Also appends an EXEC# execution-history record to the core (single) table and,
best-effort, publishes an SNS notification.
"""
import os
import json
import logging
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
codebuild = boto3.client('codebuild')
sns = boto3.client('sns')
lam = boto3.client('lambda')

TABLE_NAME = os.environ.get('DYNAMODB_TABLE_NAME', 'cloudoptix-core-table')
SNS_TOPIC_ARN = os.environ.get('SNS_TOPIC_ARN')
PROBE_FUNCTION_NAME = os.environ.get('PROBE_FUNCTION_NAME')
table = dynamodb.Table(TABLE_NAME)  # type: ignore

SUCCESS = "SUCCEEDED"

# (phase, succeeded) -> finding status. A successful apply moves to VALIDATING;
# the probe (post-apply validator) then sets VALIDATED / VALIDATION_FAILED.
_RESULT_MAP = {
    ("plan", True): "PENDING_APPROVAL",
    ("plan", False): "PLAN_FAILED",
    ("apply", True): "VALIDATING",
    ("apply", False): "APPLY_FAILED",
}


def _env_from_build(build_id):
    """Returns the environment override dict for a finished build."""
    builds = codebuild.batch_get_builds(ids=[build_id]).get('builds', [])
    if not builds:
        return {}, None
    build = builds[0]
    env = {v['name']: v['value'] for v in build.get('environment', {}).get('environmentVariables', [])}
    return env, build.get('buildStatus')


def lambda_handler(event, context):
    detail = event.get('detail', {})
    build_arn = detail.get('build-id', '')
    # EventBridge gives the ARN; batch_get_builds wants the "project:uuid" id.
    build_id = build_arn.split('build/')[-1] if 'build/' in build_arn else build_arn
    event_status = detail.get('build-status')

    if not build_id:
        logger.error("No build-id in event; ignoring.")
        return {"statusCode": 400}

    env, build_status = _env_from_build(build_id)
    status = build_status or event_status
    tenant_id = env.get('TENANT_ID')
    finding_id = env.get('ACTION_ID')
    resource_id = env.get('RESOURCE_ID')
    phase = "apply" if env.get('APPLY') == 'true' else "plan"
    succeeded = status == SUCCESS

    logger.info(f"Build {build_id} phase={phase} status={status} tenant={tenant_id} finding={finding_id}")

    if not tenant_id or not finding_id:
        logger.info("Build has no tenant/finding context (e.g. bootstrap). Nothing to reconcile.")
        return {"statusCode": 200}

    # A rollback apply is terminal remediation — record it and do NOT re-validate
    # (that would loop probe -> rollback -> probe).
    if env.get('ROLLBACK') == 'true':
        rb_status = 'ROLLED_BACK' if succeeded else 'ROLLBACK_FAILED'
        _update_finding(tenant_id, finding_id, 'rollback', rb_status, build_id)
        _record_history(tenant_id, finding_id, 'rollback', status, rb_status, build_id)
        _notify_rollback(tenant_id, finding_id, succeeded)
        return {"statusCode": 200, "finding_status": rb_status}

    new_status = _RESULT_MAP[(phase, succeeded)]

    _update_finding(tenant_id, finding_id, phase, new_status, build_id)
    _record_history(tenant_id, finding_id, phase, status, new_status, build_id)
    _notify(tenant_id, finding_id, phase, new_status, succeeded)

    # After a successful apply, hand off to the probe for post-apply validation.
    if phase == "apply" and succeeded:
        _trigger_validation(tenant_id, finding_id, resource_id)

    return {"statusCode": 200, "finding_status": new_status}


def _trigger_validation(tenant_id, finding_id, resource_id):
    if not PROBE_FUNCTION_NAME:
        logger.warning("PROBE_FUNCTION_NAME not set; skipping post-apply validation.")
        return
    try:
        lam.invoke(
            FunctionName=PROBE_FUNCTION_NAME,
            InvocationType='Event',  # async; the probe reconciles VALIDATED/VALIDATION_FAILED
            Payload=json.dumps({
                "tenant_id": tenant_id,
                "finding_id": finding_id,
                "resource_id": resource_id,
            }).encode('utf-8'),
        )
        logger.info(f"Triggered post-apply validation for finding {finding_id}")
    except ClientError as e:
        logger.warning(f"Failed to trigger probe: {e}")


def _update_finding(tenant_id, finding_id, phase, new_status, build_id):
    build_attr = {"plan": "PlanBuildStatus", "rollback": "RollbackBuildStatus"}.get(phase, "ApplyBuildStatus")
    try:
        table.update_item(
            Key={'PK': f"TENANT#{tenant_id}", 'SK': f"FINDING#{finding_id}"},
            UpdateExpression="SET #s = :s, #b = :bs, UpdatedAt = :ts",
            ConditionExpression="attribute_exists(SK)",  # never upsert a phantom finding
            ExpressionAttributeNames={'#s': 'Status', '#b': build_attr},
            ExpressionAttributeValues={
                ':s': new_status,
                ':bs': f"{build_id}:{new_status}",
                ':ts': datetime.now(timezone.utc).isoformat(),
            },
        )
        logger.info(f"Finding {finding_id} -> {new_status}")
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
            logger.warning(f"Finding {finding_id} not found; skipping status update.")
        else:
            raise


def _record_history(tenant_id, finding_id, phase, build_status, new_status, build_id):
    now = datetime.now(timezone.utc).isoformat()
    # EXEC# lives in the single core table alongside FINDING#/RESOURCE# for the tenant.
    table.put_item(Item={
        'PK': f"TENANT#{tenant_id}",
        'SK': f"EXEC#{now}#{finding_id}",
        'Type': 'ExecutionRecord',
        'FindingId': finding_id,
        'Phase': phase,
        'BuildStatus': build_status,
        'Result': new_status,
        'BuildId': build_id,
        'CreatedAt': now,
    })


def _notify(tenant_id, finding_id, phase, new_status, succeeded):
    if not SNS_TOPIC_ARN:
        return
    if phase == "plan" and succeeded:
        subject = "CloudOptix: recommendation ready for approval"
        msg = f"Tenant {tenant_id} finding {finding_id} planned successfully and is awaiting approval."
    elif not succeeded:
        subject = f"CloudOptix: {phase} failed"
        msg = f"Tenant {tenant_id} finding {finding_id} {phase} failed ({new_status}). Review CodeBuild logs."
    else:
        subject = "CloudOptix: apply complete"
        msg = f"Tenant {tenant_id} finding {finding_id} applied successfully."
    try:
        sns.publish(TopicArn=SNS_TOPIC_ARN, Subject=subject, Message=msg)
    except ClientError as e:
        logger.warning(f"SNS publish failed: {e}")


def _notify_rollback(tenant_id, finding_id, succeeded):
    if not SNS_TOPIC_ARN:
        return
    if succeeded:
        subject = "CloudOptix: change auto-rolled-back"
        msg = (f"Tenant {tenant_id} finding {finding_id} failed post-apply validation and was "
               f"automatically rolled back to the previous configuration.")
    else:
        subject = "CloudOptix: ROLLBACK FAILED"
        msg = (f"Tenant {tenant_id} finding {finding_id} failed validation AND the automatic rollback "
               f"failed. Manual intervention required — review CodeBuild logs.")
    try:
        sns.publish(TopicArn=SNS_TOPIC_ARN, Subject=subject, Message=msg)
    except ClientError as e:
        logger.warning(f"SNS publish failed: {e}")
