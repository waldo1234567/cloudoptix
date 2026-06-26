import json
import os
import boto3
import logging
from typing import Dict, Any



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
        body = json.loads(record['body'])
        tenant_id = body.get('tenant_id')
        
        if not tenant_id:
            logger.error("No tenant_id found in SQS message body.")
            continue
        
        profile = table.get_item(Key={'PK': f"TENANT#{tenant_id}", 'SK': 'PROFILE'}).get('Item', {})
        
        account_id = profile.get('TargetAccountId', '123456789012')
        region = profile.get('TargetRegion', 'ap-northeast-1')
        role_arn = profile.get('TenantRoleArn', 'arn:aws:iam::123456789012:role/CloudOptix-Tenant-Deployment-Role')
        external_id = profile.get('ExternalId', 'ext-uuid')
        
        logger.info(f"Starting inventory scan for Tenant: {tenant_id}, Account: {account_id}, Region: {region}")
        
        all_resources = []
        
        for discover_func in SCANNERS:
            try:
                resources = discover_func(tenant_id, account_id, region, role_arn, external_id)
                all_resources.extend(resources)
                logger.info(f"Discovered {len(resources)} resources via {discover_func.__module__}")
            
            except Exception as e:
                logger.error(f"Error executing scanner {discover_func.__module__}: {str(e)}", exc_info=True)
        
        if not all_resources:
            logger.info(f"No resources found for tenant {tenant_id}.")
            continue
        
        with table.batch_writer() as batch:
            for res in all_resources:
                batch.put_item(Item={
                    "PK": f"TENANT#{res.tenant_id}",
                    "SK": f"RESOURCE#{res.resource_id}",
                    "Type": "Resource",
                    "ResourceType": res.resource_type,
                    "Service": res.service,
                    "Arn": res.arn,
                    "Configuration": res.raw_metadata,
                    "Tags": res.tags,
                    "Region": res.region,
                    "AccountId": res.account_id,
                    "IsUnsafe": False,
                    "MetricSnapshot": {}
                })
        
        logger.info(f"Scan complete for {tenant_id}. Inserted {len(all_resources)} resources into DynamoDB.")

        sqs.send_message(
            QueueUrl=GRAPH_QUEUE_URL,
            MessageGroupId=tenant_id,
            MessageBody=json.dumps({"tenant_id": tenant_id})
        )
        
    return {
        "statusCode": 200,
        "body": "SQS Batch processed successfully."
    }
        
        