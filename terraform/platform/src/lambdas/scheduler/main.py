import os
import json
import logging
import boto3
from datetime import datetime, timezone, timedelta
from botocore.exceptions import ClientError


logger = logging.getLogger()
logger.setLevel(logging.INFO)

sqs = boto3.client('sqs')
dynamodb = boto3.resource('dynamodb')

SCAN_QUEUE_URL = os.environ.get('SCAN_QUEUE_URL')
ACTION_QUEUE_URL = os.environ.get('ACTION_QUEUE_URL')
TABLE_NAME = os.environ.get('DYNAMODB_TABLE_NAME', 'cloudoptix-core-table')

def get_tenant_configuration() -> list:
    table = dynamodb.Table(TABLE_NAME) # type: ignore
    tenants = []
    
    try:
        response = table.scan(
            FilterExpression="SK = :sk_val",
            ExpressionAttributeValues={":sk_val": "PROFILE"}
        )
        tenants.extend(response.get('Items', []))
        
        while 'LastEvaluatedKey' in response:
            response = table.scan(
                FilterExpression = "SK = :sk_val",
                ExpressionAttributeValues={":sk_val" : "PROFILE"}
            )
            tenants.extend(response.get('Items', []))
    
    except ClientError as e:
        logger.error(f"Failed to fetch tenant configurations: {e}")
        
    return tenants

def is_due(last_run_iso: str, interval_hours: int) -> bool:
    if not last_run_iso:
        return True
    
    try:
        last_run = datetime.fromisoformat(last_run_iso)
        next_run = last_run + timedelta(hours=interval_hours)
        return datetime.now(timezone.utc) >= next_run
    except ValueError:
        return True
    

def lambda_handler(event, context):
    if not SCAN_QUEUE_URL or not ACTION_QUEUE_URL:
        logger.critical("Missing required SQS environment variables.")
        return {"statusCode": 500, "body": "Configuration Error"}
    
    tenants = get_tenant_configuration()
    
    if not tenants:
        tenants = [{
            "PK": "TENANT#9734cae8-7021-70b5-a8c1-a21483603f66",
            "ScanIntervalHours": 24,
            "ReevaluationIntervalHours": 168, 
            "LastScanTime": None,
            "LastReevaluationTime": None
        }]
    
    now_iso = datetime.now(timezone.utc).isoformat()
    date_str = datetime.now(timezone.utc).strftime('%Y-%m-%d-%H')
    
    scans_queued = 0
    evals_queued = 0
    
    for tenant in tenants:
        tenant_id = tenant.get('PK', '').split('#')[1] if '#' in tenant.get('PK', '') else None
        if not tenant_id:
            continue
        
        scan_interval = int(tenant.get('ScanIntervalHours', 24))
        if is_due(tenant.get('LastScanTime'), scan_interval): # type: ignore
            try:
                sqs.send_message(
                    QueueUrl=SCAN_QUEUE_URL,
                    MessageBody=json.dumps({
                        "tenant_id": tenant_id,
                        "action": "INITIATE_INVENTORY_SCAN",
                        "timestamp": now_iso
                    }),
                    MessageGroupId=f"SCAN_{tenant_id}",
                    MessageDeduplicationId=f"SCAN_{tenant_id}_{date_str}"
                )
                
                scans_queued += 1
            
            except ClientError as e:
                logger.error(f"Failed to queue scan for {tenant_id}: {e}")
                
        
        eval_interval = int(tenant.get('ReevaluationIntervalHours', 168))
        if is_due(tenant.get('LastReevaluationTime'), eval_interval): # type: ignore
            try:
                sqs.send_message(
                    QueueUrl=ACTION_QUEUE_URL,
                    MessageBody=json.dumps({
                        "tenant_id": tenant_id,
                        "action": "INITIATE_PATTERN_REEVALUATION",
                        "timestamp": now_iso
                    }),
                    MessageGroupId=f"EVAL_{tenant_id}",
                    MessageDeduplicationId=f"EVAL_{tenant_id}_{date_str}"
                )
                
                evals_queued += 1
            except ClientError as e:
                logger.error(f"Failed to queue re-evaluation for {tenant_id}: {e}")
    
    logger.info(f"Orchestration complete. Scans queued: {scans_queued}, Evaluations queued: {evals_queued}")
    
    return {
        "statusCode": 200,
        "body": json.dumps({
            "scans_triggered": scans_queued,
            "evaluations_triggered": evals_queued
        })
    }