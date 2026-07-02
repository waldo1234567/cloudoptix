import json
import os
import boto3
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Any
from boto3.dynamodb.conditions import Key



from . import ec2
from . import rds
from . import lambda_function
from . import ebs
from . import s3
from . import ecs
from . import nat_gateway
from . import alb
from . import dynamodb_sc
from . import elasticache
from . import efs
from . import eip

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
sqs = boto3.client('sqs')
TABLE_NAME = os.environ.get('DYNAMODB_TABLE_NAME', 'cloudoptix-core-table')
table = dynamodb.Table(TABLE_NAME) # type: ignore
GRAPH_QUEUE_URL = os.environ.get('GRAPH_QUEUE_URL', '')


def _dynamo_safe(obj):
    """DynamoDB's resource client rejects Python floats; convert them to Decimal
    recursively (AWS describe payloads often contain floats)."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _dynamo_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_dynamo_safe(v) for v in obj]
    return obj


def _managed_map(tenant_id: str) -> dict:
    """AWS id -> {address, type} for what Terraform manages (STATEADDR# items).
    Terraform is the source of truth: we only surface resources that are in state,
    because only those can actually be remediated (the writer edits main.tf)."""
    m = {}
    kwargs = {
        'KeyConditionExpression': Key('PK').eq(f"TENANT#{tenant_id}") & Key('SK').begins_with("STATEADDR#")
    }
    while True:
        resp = table.query(**kwargs)
        for it in resp.get('Items', []):
            aws_id = str(it.get('SK', '')).split('STATEADDR#', 1)[-1]
            m[aws_id] = {'address': it.get('TerraformAddress'), 'type': it.get('TerraformType')}
        if 'LastEvaluatedKey' not in resp:
            break
        kwargs['ExclusiveStartKey'] = resp['LastEvaluatedKey']
    return m


def _existing_resource_ids(tenant_id: str) -> set:
    """AWS ids CloudOptix currently has as RESOURCE# items (to reconcile against)."""
    ids = set()
    kwargs = {
        'KeyConditionExpression': Key('PK').eq(f"TENANT#{tenant_id}") & Key('SK').begins_with("RESOURCE#"),
        'ProjectionExpression': 'SK',
    }
    while True:
        resp = table.query(**kwargs)
        for it in resp.get('Items', []):
            ids.add(str(it.get('SK', '')).split('RESOURCE#', 1)[-1])
        if 'LastEvaluatedKey' not in resp:
            break
        kwargs['ExclusiveStartKey'] = resp['LastEvaluatedKey']
    return ids


def _set_scan_status(tenant_id: str, status: str, count: int = None):
    """Reflect scan progress on the tenant profile so the UI can show SCANNING/READY."""
    expr = "SET ScanStatus = :s, LastScanTime = :t"
    values = {':s': status, ':t': datetime.now(timezone.utc).isoformat()}
    if count is not None:
        expr += ", LastResourceCount = :c"
        values[':c'] = count
    try:
        table.update_item(
            Key={'PK': f"TENANT#{tenant_id}", 'SK': 'PROFILE'},
            UpdateExpression=expr,
            ExpressionAttributeValues=values,
        )
    except Exception as e:
        logger.error(f"Failed to update scan status for {tenant_id}: {e}")

SCANNERS = [
    ec2.discover,
    ebs.discover,
    ecs.discover,
    efs.discover,
    eip.discover,
    lambda_function.discover,
    rds.discover,
    s3.discover,
    nat_gateway.discover,
    elasticache.discover,
    dynamodb_sc.discover,
    alb.discover
]

def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    logger.info("Scanner execution started from SQS.")
    
    for record in event.get('Records', []):
        tenant_id = None
        try:
            body = json.loads(record['body'])
            tenant_id = body.get('tenant_id')
        except Exception:
            logger.error("Malformed SQS message body; skipping.")
            continue

        if not tenant_id:
            logger.error("No tenant_id found in SQS message body.")
            continue

        # Whatever happens, the scan must reach a terminal status (READY/FAILED),
        # and we must NOT let the record crash — otherwise SQS redelivers it and
        # the scan loops forever (and the UI polls forever).
        try:
            _scan_tenant(tenant_id)
        except Exception as e:
            logger.error(f"Scan failed for tenant {tenant_id}: {e}", exc_info=True)
            _set_scan_status(tenant_id, "FAILED")

    return {
        "statusCode": 200,
        "body": "SQS Batch processed successfully."
    }


def _scan_tenant(tenant_id: str):
    profile = table.get_item(Key={'PK': f"TENANT#{tenant_id}", 'SK': 'PROFILE'}).get('Item', {})

    account_id = profile.get('TargetAccountId', '123456789012')
    region = profile.get('TargetRegion', 'ap-northeast-1')
    role_arn = profile.get('TenantRoleArn', 'arn:aws:iam::123456789012:role/CloudOptix-Tenant-Deployment-Role')
    external_id = profile.get('ExternalId', 'ext-uuid')

    logger.info(f"Starting inventory scan for Tenant: {tenant_id}, Account: {account_id}, Region: {region}")
    _set_scan_status(tenant_id, "SCANNING")

    # Discover live AWS resources (keyed by id). scan_complete=False if any
    # service failed — so we don't falsely flag its resources as drifted.
    found = {}
    scan_complete = True
    for discover_func in SCANNERS:
        try:
            for r in discover_func(tenant_id, account_id, region, role_arn, external_id):
                found[r.resource_id] = r
            logger.info(f"Discovered via {discover_func.__module__}")
        except Exception as e:
            scan_complete = False
            logger.error(f"Error executing scanner {discover_func.__module__}: {str(e)}", exc_info=True)

    managed_map = _managed_map(tenant_id)          # what Terraform manages (state)
    managed = set(managed_map)
    existing = _existing_resource_ids(tenant_id)   # what we currently show

    # Reconcile: drop RESOURCE# items no longer in state (destroyed / removed).
    # State-driven, so a transient describe failure can't wipe valid inventory.
    for rid in (existing - managed):
        try:
            table.delete_item(Key={'PK': f"TENANT#{tenant_id}", 'SK': f"RESOURCE#{rid}"})
        except Exception as e:
            logger.error(f"Failed to delete stale resource {rid}: {e}")
    if existing - managed:
        logger.info(f"Reconciled: removed {len(existing - managed)} resource(s) no longer in state.")

    now = datetime.now(timezone.utc).isoformat()
    active = drifted = 0
    for mid, meta in managed_map.items():
        if mid in found:
            res = found[mid]
            table.put_item(Item={
                "PK": f"TENANT#{tenant_id}",
                "SK": f"RESOURCE#{mid}",
                "Type": "Resource",
                "Status": "NEW",
                "ResourceType": res.resource_type,
                "Service": res.service,
                "Arn": res.arn,
                "RawMetadata": _dynamo_safe(res.raw_metadata),
                "Tags": _dynamo_safe(res.tags),
                "Region": res.region,
                "AccountId": res.account_id,
                "TerraformAddress": meta.get('address'),
                "IsUnsafe": False,
                "MetricSnapshot": {}
            })
            active += 1
        elif scan_complete:
            # In Terraform state but not in AWS -> drift (e.g. deleted in the console).
            if mid in existing:
                table.update_item(
                    Key={'PK': f"TENANT#{tenant_id}", 'SK': f"RESOURCE#{mid}"},
                    UpdateExpression="SET #s = :d, DriftedAt = :t",
                    ExpressionAttributeNames={'#s': 'Status'},
                    ExpressionAttributeValues={':d': 'DRIFTED', ':t': now},
                )
            else:
                tf_type = meta.get('type') or ''
                table.put_item(Item={
                    "PK": f"TENANT#{tenant_id}",
                    "SK": f"RESOURCE#{mid}",
                    "Type": "Resource",
                    "Status": "DRIFTED",
                    "ResourceType": tf_type,
                    "Service": tf_type.split('_')[0] if tf_type else "aws",
                    "Arn": "",
                    "RawMetadata": {},
                    "Tags": {},
                    "Region": region,
                    "AccountId": account_id,
                    "TerraformAddress": meta.get('address'),
                    "IsUnsafe": False,
                    "MetricSnapshot": {},
                    "DriftedAt": now,
                })
            drifted += 1
        # else: incomplete scan and not found -> leave existing item untouched.

    logger.info(f"Scan complete for {tenant_id}: {active} active, {drifted} drifted (managed {len(managed)}).")
    _set_scan_status(tenant_id, "READY", count=active + drifted)

    if active and GRAPH_QUEUE_URL:
        try:
            sqs.send_message(
                QueueUrl=GRAPH_QUEUE_URL,
                MessageGroupId=tenant_id,
                MessageBody=json.dumps({"tenant_id": tenant_id}),
            )
        except Exception as e:
            logger.error(f"Failed to enqueue graph stage for {tenant_id}: {e}")
        
        