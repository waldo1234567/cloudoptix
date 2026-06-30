import json
import os
import sys
import boto3
import logging
from typing import Dict, List, Set, Any
from collections import defaultdict, deque
from boto3.dynamodb.conditions import Key
from lambdas.scanner.auth import assume_tenant_role

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
TABLE_NAME = os.environ.get('DYNAMODB_TABLE_NAME', 'cloudoptix-core-table')
sqs = boto3.client('sqs')
METRICS_QUEUE_URL = os.environ.get('METRICS_QUEUE_URL', '')

table = dynamodb.Table(TABLE_NAME) # type: ignore

def get_tenant_resources(tenant_id: str) -> List[Dict[str, Any]]:
    """Fetches all Phase 1 inventory resources for a tenant from DynamoDB with pagination."""
    items = []
    kwargs = {
        'KeyConditionExpression' : Key('PK').eq(f"TENANT#{tenant_id}") & Key('SK').begins_with("RESOURCE#")
    }
    
    while True:
        response = table.query(**kwargs)
        items.extend(response.get('Items', []))
        if 'LastEvaluatedKey' not in response:
            break
        kwargs['ExclusiveStartKey'] = response['LastEvaluatedKey']
    return items

def identify_production_anchors(resources: List[Dict[str, Any]], region: str, role_arn:str, external_id:str) -> Set[str]:
    """
    Evaluates the 6-priority ruleset to identify production anchors.
    """
    
    anchors = set()
    
    ak, sk, token = assume_tenant_role(role_arn=role_arn, external_id= external_id)
    cw_client = boto3.client('cloudwatch', region_name=region, aws_access_key_id=ak, aws_secret_access_key=sk, aws_session_token=token)
    backup_client = boto3.client('backup', region_name=region, aws_access_key_id=ak, aws_secret_access_key=sk, aws_session_token=token)
    
    try:
        protected_resources = backup_client.list_protected_resources().get('Results', [])
        protected_arns = {res['ResourceArn'] for res in protected_resources}
        
    except Exception as e:
        logger.error(f"Failed to fetch backup resources: {e}")
        protected_arns = set()
    
    
    for res in resources:
        res_id = res['SK'].split('RESOURCE#', 1)[1]
        arn = res.get('Arn')
        tags= res.get('Tags', {})
        meta = res.get('RawMetadata', {})
        resource_type = res.get('ResourceType')
        
        env_tag = str(tags.get('Environment', '')).lower()
        if env_tag in ['production', 'prod']:
            anchors.add(res_id)
            continue
        
        if resource_type == 'load_balancer' and meta.get('Scheme') == 'internet-facing':
            anchors.add(res_id)
            continue
        
        if resource_type == 'db-instance' and meta.get('MultiAZ') is True:
            anchors.add(res_id)
            continue
        
        if tags.get('aws:autoscaling:groupName'):
            anchors.add(res_id)
            continue
        
        try:
            alarms = cw_client.describe_alarms_for_metric(MetricName = 'CPUUtilization', Namespace = 'AWS/EC2', Dimensions=[{'Name' : 'InstanceId', 'Value': res_id}])
            if alarms.get('MetricAlarms'):
                anchors.add(res_id)
                continue
        except Exception:
            pass
        
        
        if arn in protected_arns:
            anchors.add(res_id)
            continue
    
    return anchors

def normalize_tg_id(arn: str) -> str:
    """Extracts the unique ID part from a Target Group ARN for consistent mapping."""
    return arn.split(':')[-1] if arn else ""

def build_edges(resources: List[Dict[str, Any]]) -> List[tuple]:
    """
    Maps provable dependency relationships between resources.
    """
    
    edges = []
    
    for res in resources:
        
        src_id = res['SK'].split('RESOURCE#', 1)[1]
        meta = res.get('RawMetadata', {})
        res_type = res.get('ResourceType')
        
        if res_type == 'instance':
            if meta.get('VpcId'):
                edges.append((src_id, meta['VpcId']))
            if meta.get('SubnetId'):
                edges.append((src_id, meta['SubnetId']))
            for sg in meta.get('SecurityGroups', []):
                edges.append((src_id, sg.get('GroupId')))
            if meta.get('IamInstanceProfile'):
                edges.append((src_id, meta['IamInstanceProfile']['Id']))
            for mapping in meta.get('BlockDeviceMappings', []):
                vol_id = mapping.get('Ebs', {}).get('VolumeId')
                if vol_id:
                    edges.append((src_id, vol_id))  
        
        elif res_type == 'db-instance':
            if meta.get('VpcId'):
                edges.append((src_id, meta['VpcId']))
            for sg in meta.get('VpcSecurityGroups', []):
                edges.append((src_id, sg.get('VpcSecurityGroupId')))
                
        elif res_type == 'function':
            vpc_config = meta.get('VpcConfig', {})
            if vpc_config.get('VpcId'):
                edges.append((src_id, vpc_config['VpcId']))
            for sg in vpc_config.get('SecurityGroupIds',[]):
                edges.append((src_id, sg))
        
        elif res_type == 'loadbalancer':
            for tg_arn in meta.get('TargetGroupArns', []):
                edges.append((src_id, normalize_tg_id(tg_arn)))
                
        elif res_type == 'service':
            for tg_arn in meta.get('TargetGroupArns', []):
                edges.append((src_id, normalize_tg_id(tg_arn)))
                
        elif res_type == 'natgateway':
            if meta.get('VpcId'):
                edges.append((src_id, meta['VpcId']))
            if meta.get('SubnetId'):
                edges.append((src_id, meta['SubnetId']))
                
            for rtb_id in meta.get('AssociatedRouteTables', []):
                edges.append((src_id, rtb_id))
    
    
    return [edge for edge in edges if edge[0] and edge[1]]

def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Triggered by SQS or Step Functions after the inventory scan completes.
    """
    tenant_id = event['tenant_id']
    region = event['region']
    role_arn = event['role_arn']
    external_id = event['external_id']
    
    logger.info(f"Starting Graph Traversal for Tenant: {tenant_id}")
    
    resources = get_tenant_resources(tenant_id)
    
    if not resources:
        
        return {
            "statusCode": 200, 
            "status": "NO_RESOURCES",
            "anchors_found": 0,
            "unsafe_resources": 0,
            "edges_mapped": 0
        }
        
    edges = build_edges(resources)
    adjacency_list = defaultdict(list)
    
    for src,dst in edges:
        adjacency_list[src].append(dst)
        adjacency_list[dst].append(src)
    
    anchors = identify_production_anchors(resources, region, role_arn, external_id)
    
    logger.info(f"Identified {len(anchors)} production anchors.")
    
    unsafe_set = set(anchors)
    
    queue = deque(anchors)
    
    while queue:
        current = queue.popleft()
        for neighbor in adjacency_list.get(current, []):
            if neighbor not in unsafe_set:
                unsafe_set.add(neighbor)
                queue.append(neighbor)
                
    logger.info(f"Graph traversal marked {len(unsafe_set)} resources as UNSAFE.")
    
    
    with table.batch_writer() as batch:
        for src, dst in edges:
            batch.put_item(Item = {
                "PK" :f"TENANT#{tenant_id}",
                "SK" :f"EDGE#{src}#{dst}",
                "Type": "DependencyEdge",
                "Source" : src,
                "Destination": dst
            })
    
    for res in resources:
        res_id = res['SK'].split('RESOURCE#', 1)[1]
        is_unsafe = res_id in unsafe_set
        
        table.update_item(
            Key = {'PK': f"TENANT#{tenant_id}", 'SK' : f"RESOURCE#{res_id}"},
            UpdateExpression = "SET IsUnsafe = :val",
            ExpressionAttributeValues={':val': is_unsafe}
        )
    
    sqs.send_message(
        QueueUrl=METRICS_QUEUE_URL,
        MessageGroupId=tenant_id,
        MessageBody=json.dumps({"tenant_id": tenant_id})
    )
    
    return {
        "statusCode": 200,
        "status": "SUCCESS",
        "anchors_found": len(anchors),
        "unsafe_resources": len(unsafe_set),
        "edges_mapped": len(edges)
    }