import os
import json
import logging
import boto3
from datetime import datetime, timezone

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
sqs = boto3.client('sqs')
TABLE_NAME = os.environ['DYNAMODB_TABLE_NAME']
SCAN_QUEUE_URL = os.environ['SCAN_QUEUE_URL']
table = dynamodb.Table(TABLE_NAME)


def lambda_handler(event, context):
    """POST /api/v1/tenants/{id}/scan

    Manually enqueue an inventory scan for the tenant. Drops a message on the
    (FIFO) scan queue the scanner consumes, and flips the tenant profile to
    SCANNING so the UI reflects it immediately.
    """
    try:
        tenant_id = (event.get('pathParameters') or {}).get('id')
        if not tenant_id:
            return _response(400, {"error": "Missing tenant id in path parameters."})

        profile = table.get_item(
            Key={'PK': f"TENANT#{tenant_id}", 'SK': "PROFILE"}
        ).get('Item')
        if not profile:
            return _response(404, {"error": "Tenant not found. Register first."})

        now = datetime.now(timezone.utc).isoformat()
        # Include a timestamp so content-based dedup on the FIFO queue doesn't drop
        # back-to-back manual scans.
        sqs.send_message(
            QueueUrl=SCAN_QUEUE_URL,
            MessageGroupId=tenant_id,
            MessageBody=json.dumps({"tenant_id": tenant_id, "requested_at": now, "source": "manual"}),
        )

        table.update_item(
            Key={'PK': f"TENANT#{tenant_id}", 'SK': "PROFILE"},
            UpdateExpression="SET ScanStatus = :s, LastScanRequestedAt = :t",
            ExpressionAttributeValues={':s': 'SCANNING', ':t': now},
        )

        return _response(202, {"status": "SCANNING", "message": "Scan queued.", "requested_at": now})

    except Exception as e:
        logger.error(f"Scan trigger API Error: {e}", exc_info=True)
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
