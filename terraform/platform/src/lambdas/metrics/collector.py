import json
import os
import sys
import boto3
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Any
from boto3.dynamodb.conditions import Key,Attr
from lambdas.scanner.auth import assume_tenant_role

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
sqs = boto3.client('sqs')
TABLE_NAME = os.environ.get('DYNAMODB_TABLE_NAME', 'cloudoptix-core-table')
RULES_QUEUE_URL = os.environ.get('RULES_QUEUE_URL', '')
table = dynamodb.Table(TABLE_NAME)

# Finalized Metric Registry for Deterministic Rule Evaluation
METRIC_REGISTRY = {
    'instance': [
        {'Namespace': 'AWS/EC2', 'MetricName': 'CPUUtilization'},
        {'Namespace': 'AWS/EC2', 'MetricName': 'NetworkOut'},
        {'Namespace': 'AWS/EC2', 'MetricName': 'NetworkIn'},
        {'Namespace': 'AWS/EC2', 'MetricName': 'EBSReadOps'},
        {'Namespace': 'AWS/EC2', 'MetricName': 'EBSWriteOps'},
        {'Namespace': 'AWS/EC2', 'MetricName': 'CPUCreditBalance'}, 
        {'Namespace': 'AWS/EC2', 'MetricName': 'CPUCreditUsage'},
        {'Namespace': 'AWS/EC2', 'MetricName': 'StatusCheckFailed'} 
    ],
    'db-instance': [
        {'Namespace': 'AWS/RDS', 'MetricName': 'CPUUtilization'},
        {'Namespace': 'AWS/RDS', 'MetricName': 'DatabaseConnections'},
        {'Namespace': 'AWS/RDS', 'MetricName': 'ReadIOPS'},
        {'Namespace': 'AWS/RDS', 'MetricName': 'WriteIOPS'},
        {'Namespace': 'AWS/RDS', 'MetricName': 'FreeableMemory'}, 
        {'Namespace': 'AWS/RDS', 'MetricName': 'SwapUsage'},      
        {'Namespace': 'AWS/RDS', 'MetricName': 'BurstBalance'}    
    ],
    'function': [
        {'Namespace': 'AWS/Lambda', 'MetricName': 'Invocations'},
        {'Namespace': 'AWS/Lambda', 'MetricName': 'Duration'},
        {'Namespace': 'AWS/Lambda', 'MetricName': 'Errors'},
        {'Namespace': 'AWS/Lambda', 'MetricName': 'Throttles'}    
    ],
    'volume': [
        {'Namespace': 'AWS/EBS', 'MetricName': 'VolumeReadOps'},
        {'Namespace': 'AWS/EBS', 'MetricName': 'VolumeWriteOps'},
        {'Namespace': 'AWS/EBS', 'MetricName': 'VolumeIdleTime'} 
    ],
    'loadbalancer': [
        {'Namespace': 'AWS/ApplicationELB', 'MetricName': 'RequestCount'},
        {'Namespace': 'AWS/ApplicationELB', 'MetricName': 'TargetConnectionErrorCount'},
        {'Namespace': 'AWS/ApplicationELB', 'MetricName': 'UnHealthyHostCount'},
        {'Namespace': 'AWS/ApplicationELB', 'MetricName': 'HealthyHostCount'} 
    ],
    'natgateway': [
        {'Namespace': 'AWS/NATGateway', 'MetricName': 'BytesOutToDestination'},
        {'Namespace': 'AWS/NATGateway', 'MetricName': 'BytesInFromSource'},
        {'Namespace': 'AWS/NATGateway', 'MetricName': 'ActiveConnectionCount'}
    ],
    'cluster': [ 
        {'Namespace': 'AWS/ElastiCache', 'MetricName': 'CPUUtilization'},
        {'Namespace': 'AWS/ElastiCache', 'MetricName': 'CacheHits'},
        {'Namespace': 'AWS/ElastiCache', 'MetricName': 'CacheMisses'},
        {'Namespace': 'AWS/ElastiCache', 'MetricName': 'CurrConnections'},
        {'Namespace': 'AWS/ElastiCache', 'MetricName': 'Evictions'}       
    ]
}

def get_safe_resources(tenant_id: str) -> List[Dict[str, Any]]:
    items = []
    kwargs = {
        'KeyConditionExpression': Key('PK').eq(f"TENANT#{tenant_id}") & Key('SK').begins_with("RESOURCE#"),
        'FilterExpression' : Attr('IsUnsafe').eq(False)
    }
    
    while True:
        response = table.query(**kwargs)
        items.extend(response.get('Items', []))
        if 'LastEvaluatedKey' not in response:
            break
        kwargs['ExclusiveStartKey'] = response['LastEvaluatedKey']
    return items


def build_metric_queries(resource_id: str, resource_type: str) -> List[Dict[str, Any]]:
    queries = []
    metrics = METRIC_REGISTRY.get(resource_type, [])
    
    dimension_keys = {
        'instance': 'InstanceId',
        'db-instance': 'DBInstanceIdentifier',
        'function': 'FunctionName',
        'volume': 'VolumeId',
        'loadbalancer': 'LoadBalancer',
        'natgateway': 'NatGatewayId'
    }
    
    dim_key =  dimension_keys.get(resource_type)
    
    if not dim_key:
        return queries
    
    query_idx = 0
    
    for metric in metrics:
        queries.append({
            'Id' : f"m_{query_idx}_p99",
            'MetricStat' : {
                'Metric' : {
                    'NameSpace' : metric['Namespace'],
                    'MetricName' : metric['MetricName'],
                    'Dimensions' : [{'Name': dim_key, 'Value' : resource_id}]
                },
                'Period': 86400,
                'Stat': 'p99'
            },
            'ReturnData': True
        })
        
        queries.append({
            'Id' : f"m_{query_idx}_avg",
            'MetricStat': {
                'Metric' : {
                    'NameSpace' : metric['Namespace'],
                    'MetricName': metric['MetricName'],
                    'Dimension': [{'Name': dim_key, 'Value': resource_id}]
                },
                'Period': 86400,
                'Stat': 'Average'
            },
            'ReturnData': True
        })
        
        queries.append({
            'Id' : f"m_{query_idx}_max",
            'MetricStat': {
                'Metric' : {
                    'Namespace': metric['Namespace'],
                    'MetricName': metric['MetricName'],
                    'Dimensions': [{'Name': dim_key, 'Value': resource_id}]
                },
                'Period': 86400,
                'Stat': 'Maximum'
            },
            'ReturnData': True  
        })

        query_idx += 1
        
    return queries

def process_metrics(cw_client, resource: Dict[str, Any], start_time : datetime, end_time: datetime) -> Dict[str, Any]:
    """Executes the CloudWatch query and normalizes the results."""
    res_id = resource['SK'].split('RESOURCE#', 1)[1]
    res_type = resource.get('ResourceType')
    
    queries = build_metric_queries(res_id, res_type) # type: ignore
    if not queries:
        return {}
    
    try: 
        response = cw_client.get_metric_data(
            MetricDataQueries=queries,
            StartTime = start_time,
            EndTime = end_time
        )

        results = {}
        for metric_data in response.get('MetricDataResults', []):
            query_id = metric_data['Id']
            values = metric_data.get('Value', [])
            
            idx = int(query_id.split('_')[1])
            stat = query_id.split('_')[2]
            metric_name = METRIC_REGISTRY[res_type][idx]['MetricName'] # type: ignore
            
            if metric_name not in results:
                results[metric_name] = {}
            
            results[metric_name][stat] = sum(values) / len(values) if values else 0.0
        
        return results
    except Exception as e:
        logger.error(f"Failed to fetch metrics for {res_id}: {e}")
        return {}
    

def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Triggered after Graph Construction.
    """
    tenant_id = event['tenant_id']
    region = event['region']
    role_arn = event['role_arn']
    external_id = event['external_id']
    
    logger.info(f"Starting Metric Collection for Tenant: {tenant_id}")
    
    
    safe_resources = get_safe_resources(tenant_id)
    if not safe_resources:
        return {"status" :"NO_SAFE_RESOURCES", "collected_count": 0}
    
    ak, sk, token = assume_tenant_role(role_arn, external_id)

    cw_client = boto3.client('cloudwatch', region_name=region, aws_access_key_id = ak, aws_secret_access_key=sk, aws_session_token=token)
    end_time = datetime.utcnow()
    
    start_time = end_time - timedelta(days=14)
    
    collected_count = 0
    
    with table.batch_writer() as batch:
        for resource in safe_resources:
            metrics = process_metrics(cw_client, resource, start_time, end_time)
            
            if metrics:
                res_id = resource['SK'].split('RESOURCE#', 1)[1]
                table.update_item(
                    Key = {'PK': f"TENANT#{tenant_id}", 'SK' : f"RESOURCE#{res_id}"},
                    UpdateExpression = "SET MetricSnapshot = :val",
                    ExpressionAttributeValues = {':val': metrics}
                )                    
                collected_count += 1
    
    sqs.send_message(
        QueueUrl=RULES_QUEUE_URL,
        MessageGroupId=tenant_id,
        MessageBody=json.dumps({"tenant_id": tenant_id})
    )
    
    return {
        "statusCode": 200,
        "status": "SUCCESS",
        "safe_resources_evaluated": len(safe_resources),
        "resources_with_metrics": collected_count
    }