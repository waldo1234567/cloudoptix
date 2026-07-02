import os
import json
import logging
import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
s3 = boto3.client('s3')
TABLE_NAME = os.environ['DYNAMODB_TABLE_NAME']
CONFIG_BUCKET = os.environ['CONFIG_BUCKET']
STATE_BUCKET = os.environ['STATE_BUCKET']
table = dynamodb.Table(TABLE_NAME)


def lambda_handler(event, context):
    """One DELETE handler, dispatched by what's in the path/query:

      DELETE /api/v1/tenants/{id}                              -> delete whole tenant
      DELETE /api/v1/tenants/{id}/recommendations/{rec_id}     -> dismiss a finding
      DELETE /api/v1/tenants/{id}/resources?resource_id=...    -> delete one resource

    Deleting a tenant removes its CloudOptix data + S3 workspace/state only — it
    does NOT touch the real AWS resources in the tenant account.
    """
    try:
        pp = event.get('pathParameters') or {}
        qs = event.get('queryStringParameters') or {}
        tenant_id = pp.get('id')
        rec_id = pp.get('rec_id')
        resource_id = qs.get('resource_id')

        if not tenant_id:
            return _response(400, {"error": "Missing tenant id in path parameters."})

        if rec_id:
            table.delete_item(Key={'PK': f"TENANT#{tenant_id}", 'SK': f"FINDING#{rec_id}"})
            return _response(200, {"deleted": "finding", "id": rec_id})

        if resource_id:
            table.delete_item(Key={'PK': f"TENANT#{tenant_id}", 'SK': f"RESOURCE#{resource_id}"})
            return _response(200, {"deleted": "resource", "id": resource_id})

        _delete_tenant(tenant_id)
        return _response(200, {"deleted": "tenant", "id": tenant_id})

    except Exception as e:
        logger.error(f"Delete API Error: {e}", exc_info=True)
        return _response(500, {"error": "Internal Server Error."})


def _delete_tenant(tenant_id: str):
    # 1. Every DynamoDB item under this tenant partition.
    kwargs = {
        'KeyConditionExpression': Key('PK').eq(f"TENANT#{tenant_id}"),
        'ProjectionExpression': 'PK, SK',
    }
    with table.batch_writer() as batch:
        while True:
            resp = table.query(**kwargs)
            for it in resp.get('Items', []):
                batch.delete_item(Key={'PK': it['PK'], 'SK': it['SK']})
            if 'LastEvaluatedKey' not in resp:
                break
            kwargs['ExclusiveStartKey'] = resp['LastEvaluatedKey']

    # 2. S3 workspace (versioned config bucket) — delete all versions + markers.
    _delete_prefix_versions(CONFIG_BUCKET, f"{tenant_id}/")
    # 3. S3 state (non-versioned state bucket).
    _delete_prefix(STATE_BUCKET, f"{tenant_id}/")
    logger.info(f"Deleted tenant {tenant_id} (dynamo items + S3 workspace/state).")


def _delete_prefix_versions(bucket: str, prefix: str):
    paginator = s3.get_paginator('list_object_versions')
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        objs = [
            {'Key': v['Key'], 'VersionId': v['VersionId']}
            for v in page.get('Versions', []) + page.get('DeleteMarkers', [])
        ]
        if objs:
            s3.delete_objects(Bucket=bucket, Delete={'Objects': objs})


def _delete_prefix(bucket: str, prefix: str):
    paginator = s3.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        objs = [{'Key': o['Key']} for o in page.get('Contents', [])]
        if objs:
            s3.delete_objects(Bucket=bucket, Delete={'Objects': objs})


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }
