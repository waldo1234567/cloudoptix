import os
import json
import logging
from decimal import Decimal
import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
TABLE_NAME = os.environ['DYNAMODB_TABLE_NAME']
table = dynamodb.Table(TABLE_NAME)


class DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)


def lambda_handler(event, context):
    """GET /api/v1/tenants/{id}/resources

    Returns the tenant's discovered infrastructure (RESOURCE# items written by
    the scanner) plus the tenant-level scan status so the UI can show SCANNING.
    """
    try:
        tenant_id = (event.get('pathParameters') or {}).get('id')
        if not tenant_id:
            return _response(400, {"error": "Missing tenant id in path parameters."})

        profile = table.get_item(
            Key={'PK': f"TENANT#{tenant_id}", 'SK': "PROFILE"}
        ).get('Item') or {}
        scan_status = profile.get('ScanStatus', 'NEVER')

        items = []
        kwargs = {
            'KeyConditionExpression': Key('PK').eq(f"TENANT#{tenant_id}") & Key('SK').begins_with("RESOURCE#")
        }
        while True:
            resp = table.query(**kwargs)
            items.extend(resp.get('Items', []))
            if 'LastEvaluatedKey' not in resp:
                break
            kwargs['ExclusiveStartKey'] = resp['LastEvaluatedKey']

        resources = []
        for it in items:
            sk = str(it.get('SK', ''))
            resources.append({
                "resource_id": sk.replace("RESOURCE#", ""),
                "resource_type": it.get('ResourceType'),
                "service": it.get('Service'),
                "region": it.get('Region'),
                "arn": it.get('Arn'),
                "status": it.get('Status', 'NEW'),
                "tags": it.get('Tags', {}),
            })

        resources.sort(key=lambda r: (r['service'] or '', r['resource_id'] or ''))

        return _response(200, {
            "tenant_id": tenant_id,
            "scan_status": scan_status,
            "reconcile_status": profile.get('ReconcileStatus'),
            "count": len(resources),
            "resources": resources,
        })

    except Exception as e:
        logger.error(f"Resources API Error: {e}", exc_info=True)
        return _response(500, {"error": "Internal Server Error."})


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body, cls=DecimalEncoder),
    }
