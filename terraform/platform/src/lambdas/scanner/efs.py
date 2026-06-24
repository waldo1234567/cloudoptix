import boto3
from typing import List
from .auth import assume_tenant_role
from rules.models import Resource

def discover(tenant_id: str, account_id: str, region: str, role_arn: str, external_id: str) -> List[Resource]:
    ak, sk, token = assume_tenant_role(role_arn, external_id)
    
    efs_client = boto3.client(
        'efs',
        region_name=region,
        aws_access_key_id=ak,
        aws_secret_access_key=sk,
        aws_session_token=token
    )
    
    resources = []
    paginator = efs_client.get_paginator('describe_file_systems')
    
    for page in paginator.paginate():
        for fs in page.get('FileSystems', []):
            
            fs_id = fs['FileSystemId']
            arn = f"arn:aws:elasticfilesystem:{region}:{account_id}:file-system/{fs_id}"
            
            tags = {tag['Key']: tag['Value'] for tag in fs.get('Tags', [])}
            
            res = Resource(
                tenant_id=tenant_id,
                account_id=account_id,
                region=region,
                service="efs",
                resource_type="filesystem",
                resource_id=fs_id,
                arn=arn,
                tags=tags,
                raw_metadata={
                    "LifeCycleState": fs['LifeCycleState'],
                    "NumberOfMountTargets": fs['NumberOfMountTargets'],
                    "PerformanceMode": fs['PerformanceMode'],
                    "ThroughputMode": fs.get('ThroughputMode', 'bursting'),
                    "SizeInBytes": fs['SizeInBytes']['Value'],
                    "CreationTime": fs['CreationTime'].isoformat()
                }
            )
            resources.append(res)
            
    return resources