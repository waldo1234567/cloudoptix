import os
import json
import logging
import boto3
from decimal import Decimal

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
    """GET /api/v1/tenants/{id}/findings/{rec_id}/status

    Lightweight poll target for the dashboard: returns just the live status of a
    single finding plus the per-phase build statuses and any validation detail,
    so the UI can watch an approval through APPLYING -> VALIDATING -> VALIDATED
    without re-fetching the whole recommendations list.
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
            return _response(404, {"error": "Finding not found."})

        return _response(200, {
            "id": rec_id,
            "status": finding.get('Status', 'NEW'),
            "plan_build_status": finding.get('PlanBuildStatus'),
            "apply_build_status": finding.get('ApplyBuildStatus'),
            "rollback_build_status": finding.get('RollbackBuildStatus'),
            "validation_detail": finding.get('ValidationDetail'),
        })

    except Exception as e:
        logger.error(f"Finding status API Error: {e}", exc_info=True)
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
