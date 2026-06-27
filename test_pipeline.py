import json
import boto3
import time
from decimal import Decimal

TARGET_ACCOUNT_ID = "247448832186"
HUB_ACCOUNT_ID = "289835835123"

SNS_TOPIC_ARN = f"arn:aws:sns:ap-northeast-1:{HUB_ACCOUNT_ID}:cloudoptix-alerts"
TENANT_ID = "mock-tenant-e2e-001"
TABLE_NAME = "cloudoptix-core-table"
REGION = "ap-northeast-1"
ROLE_ARN = f"arn:aws:iam::{TARGET_ACCOUNT_ID}:role/CloudOptix-Tenant-Deployment-Role"

dynamodb = boto3.resource('dynamodb', region_name=REGION)
lambda_client = boto3.client('lambda', region_name=REGION)
table = dynamodb.Table(TABLE_NAME) # type: ignore

def float_to_decimal(obj):
    if isinstance(obj, float): return Decimal(str(obj))
    if isinstance(obj, dict): return {k: float_to_decimal(v) for k, v in obj.items()}
    return obj

def seed_database():
    print(f"[*] Seeding Profile for Tenant {TENANT_ID}...")
    with table.batch_writer() as batch:
        batch.put_item(Item={
            "PK": f"TENANT#{TENANT_ID}", "SK": "PROFILE",
            "TargetAccountId": TARGET_ACCOUNT_ID, "TargetRegion": REGION,
            "TenantRoleArn": ROLE_ARN, "ExternalId": "cloudoptix-ext-test-001"
        })
        
        mocks = [
            {"id": "i-mock-ec2-idle", "type": "instance", "meta": {"InstanceType": "t3.large"}, 
             "metrics": {"CPUUtilization": {"Average": 1.2, "p99": 3.5}, "NetworkIn": {"Average": 500}, "NetworkOut": {"Average": 500}}},
            
            # 2. RDS: ABANDONED -> Should generate Tier 3 remove with snapshot
            {"id": "mock-db-abandoned", "type": "db-instance", "meta": {"DBInstanceClass": "db.m5.large"}, 
             "metrics": {"DatabaseConnections": {"Maximum": 0}, "CPUUtilization": {"Average": 0.5, "p99": 0.8}}},
            
            # 3. EBS: UNATTACHED -> Should trigger Tier 1 Delete (Will fail safely because vol is fake)
            {"id": "vol-mock-unattached", "type": "volume", "meta": {"State": "available"}, "metrics": {}},
            
            # 4. ALB: ABANDONED -> Should generate Tier 3 remove
            {"id": "mock-alb-abandoned", "type": "loadbalancer", "meta": {}, 
             "metrics": {"RequestCount": {"Maximum": 0, "Average": 0}}},
             
            # 5. NAT Gateway: ABANDONED -> Should generate Tier 3 remove
            {"id": "nat-mock-abandoned", "type": "natgateway", "meta": {}, 
             "metrics": {"ActiveConnectionCount": {"Maximum": 0}, "BytesOutToDestination": {"Average": 0}}},
             
            # 6. DynamoDB: ABANDONED -> Should generate Tier 3 remove
            {"id": "mock-table-abandoned", "type": "table", "meta": {"BillingMode": "PROVISIONED"}, 
             "metrics": {"ConsumedReadCapacityUnits": {"p99": 0}, "ConsumedWriteCapacityUnits": {"p99": 0}}},
             
            # 7. Lambda: ABANDONED -> Should generate Tier 3 remove
            {"id": "mock-func-abandoned", "type": "function", "meta": {}, 
             "metrics": {"Invocations": {"Sum": 0}, "Throttles": {"Maximum": 0}}},
             
            # 8. EIP: UNATTACHED -> Should generate Tier 3 release
            {"id": "eipalloc-mock-unattached", "type": "eipalloc", "meta": {"is_attached": False}, "metrics": {}},
            
            # 9. EFS: ABANDONED -> Should generate Tier 3 remove
            {"id": "fs-mock-abandoned", "type": "filesystem", "meta": {}, 
             "metrics": {"ClientConnections": {"Maximum": 0}}},
             
            # 10. ElastiCache: ABANDONED -> Should generate Tier 3 remove
            {"id": "mock-cache-abandoned", "type": "cluster", "meta": {"CacheNodeType": "cache.r5.large"}, 
             "metrics": {"CurrConnections": {"Maximum": 0}}},
             
            # 11. ECS: ABANDONED (Scaled to 0) -> Should generate Tier 3 App Auto Scaling
            {"id": "mock-service-abandoned", "type": "service", "meta": {"DesiredCount": 2, "ClusterName": "mock-cluster"}, 
             "metrics": {"CPUUtilization": {"Average": 0.1, "p99": 0.2}}},
             
            # 12. S3: EMPTY BUCKET -> Should generate Tier 3 remove
            {"id": "mock-bucket-empty", "type": "bucket", "meta": {"BucketSizeBytes": 0, "NumberOfObjects": 0}, "metrics": {}},
        ]
        
        for m in mocks:
            batch.put_item(Item=float_to_decimal({
                "PK": f"TENANT#{TENANT_ID}", "SK": f"RESOURCE#{m['id']}",
                "Type": "Resource", "ResourceType": m['type'], "IsUnsafe": False,
                "RawMetadata": m['meta'], "MetricSnapshot": m['metrics']
            }))
    print("[+] Seed complete: 12 mock resources injected.")
    
def trigger_rules_engine():
    print(f"[*] Simulating Metrics Collector handoff -> Invoking Rules Engine Lambda...")
    
    mock_sqs_event = {
        "Records": [{
            "body": json.dumps({"tenant_id": TENANT_ID})
        }]
    }
    
    try:
        response = lambda_client.invoke(
            FunctionName='CloudOptix-Rules-Engine',
            InvocationType='Event',
            Payload=json.dumps(mock_sqs_event)
        )
        print("[+] Rules Engine invoked successfully.")
        print("[!] The cascade has started. The Rules Engine is evaluating the matrix and pushing to the Action Queue.")
    except Exception as e:
        print(f"[-] Failed to invoke Rules Engine: {e}")

if __name__ == "__main__":
    time.sleep(2)
    trigger_rules_engine()
        