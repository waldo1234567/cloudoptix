import boto3
from typing import List
from .auth import assume_tenant_role
from rules.models import Resource

def discover(tenant_id: str, account_id: str, region: str, role_arn: str, external_id: str) -> List[Resource]:
    ak, sk, token = assume_tenant_role(role_arn, external_id)
    
    ddb_client = boto3.client(
        'dynamodb',
        region_name=region,
        aws_access_key_id=ak,
        aws_secret_access_key=sk,
        aws_session_token=token
    )
    
    resources = []
    paginator = ddb_client.get_paginator('list_tables')
    
    
    for page in paginator.paginate():
        for table_name in page.get('TableNames', []):
            
            desc_response = ddb_client.describe_table(TableName = table_name)
            table = desc_response['Table']
            arn = table['TableArn']
            
            try:
                tag_response = ddb_client.list_tags_of_resource(ResourceArn=arn)
                tags = {tag['Key']: tag['Value'] for tag in tag_response.get('Tags', [])}
            except Exception:
                tags = {}
            
            billing_mode = table.get('BillingModeSummary', {}).get('BillingMode', 'PROVISIONED')
            
            res = Resource(
                tenant_id=tenant_id,
                account_id=account_id,
                region=region,
                service="dynamodb",
                resource_type="table",
                resource_id=table_name,
                arn=arn,
                tags=tags,
                raw_metadata={
                    "TableStatus": table['TableStatus'],
                    "ItemCount": table.get('ItemCount', 0),
                    "TableSizeBytes": table.get('TableSizeBytes', 0),
                    "BillingMode": billing_mode,
                    "ProvisionedThroughput": table.get('ProvisionedThroughput', {}),
                    "CreationDateTime": table['CreationDateTime'].isoformat()
                }
            )
            resources.append(res)
            
    return resources