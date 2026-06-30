"""
CloudOptix State Parser.

Triggered by S3 ObjectCreated on the tenant tfstate bucket. Reads the
terraform.tfstate, maps every managed resource's real AWS id to its terraform
address, and persists the mapping so the HCL Writer can resolve the
__TF_ADDRESS__ placeholder.

DynamoDB item written per resource:
  PK = TENANT#{tenant_id}
  SK = STATEADDR#{aws_resource_id}
  TerraformAddress = "aws_instance.web"   (incl. [index] for count/for_each)

The tfstate object key is laid out as: {tenant_id}/{resource_id}/terraform.tfstate
(see buildspec.yml), so the tenant id is the first path segment.
"""
import os
import json
import logging
from urllib.parse import unquote_plus
from datetime import datetime, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client('s3')
dynamodb = boto3.resource('dynamodb')
TABLE_NAME = os.environ.get('DYNAMODB_TABLE_NAME', 'cloudoptix-core-table')
table = dynamodb.Table(TABLE_NAME)  # type: ignore


def _format_address(res_type: str, res_name: str, index_key) -> str:
    """Builds the terraform address, including the count/for_each index when present."""
    address = f"{res_type}.{res_name}"
    if index_key is None:
        return address
    if isinstance(index_key, int):
        return f"{address}[{index_key}]"
    # for_each string keys are quoted in terraform addresses
    return f'{address}["{index_key}"]'


def _extract_mappings(state: dict):
    """Yields (aws_resource_id, terraform_address, terraform_type) for managed resources."""
    for resource in state.get('resources', []):
        if resource.get('mode') != 'managed':
            continue  # skip data sources

        res_type = resource.get('type')
        res_name = resource.get('name')
        if not res_type or not res_name:
            continue

        for inst in resource.get('instances', []):
            attrs = inst.get('attributes', {}) or {}
            aws_id = attrs.get('id') or attrs.get('arn')
            if not aws_id:
                logger.warning(f"No id/arn for {res_type}.{res_name}; skipping instance.")
                continue

            address = _format_address(res_type, res_name, inst.get('index_key'))
            yield str(aws_id), address, res_type


def _tenant_id_from_key(key: str) -> str:
    return key.split('/')[0] if '/' in key else ''


def lambda_handler(event, context):
    written = 0

    for record in event.get('Records', []):
        bucket = record['s3']['bucket']['name']
        key = unquote_plus(record['s3']['object']['key'])

        tenant_id = _tenant_id_from_key(key)
        if not tenant_id:
            logger.error(f"Could not derive tenant_id from key '{key}'. Skipping.")
            continue

        logger.info(f"Parsing state for tenant {tenant_id} from s3://{bucket}/{key}")

        try:
            obj = s3.get_object(Bucket=bucket, Key=key)
            state = json.loads(obj['Body'].read().decode('utf-8'))
        except Exception as e:
            logger.error(f"Failed to read/parse state {key}: {e}", exc_info=True)
            continue

        now = datetime.now(timezone.utc).isoformat()

        with table.batch_writer() as batch:
            for aws_id, address, res_type in _extract_mappings(state):
                batch.put_item(Item={
                    'PK': f"TENANT#{tenant_id}",
                    'SK': f"STATEADDR#{aws_id}",
                    'Type': 'StateAddress',
                    'TerraformAddress': address,
                    'TerraformType': res_type,
                    'UpdatedAt': now,
                })
                written += 1

        logger.info(f"Mapped {written} resource addresses for tenant {tenant_id}.")

    return {"statusCode": 200, "mapped": written}
