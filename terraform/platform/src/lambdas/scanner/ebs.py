import boto3
from typing import List
from .auth import assume_tenant_role
from rules.models import Resource

def discover(tenant_id: str, account_id: str, region: str, role_arn: str, external_id: str) -> List[Resource]:
    ak, sk, token = assume_tenant_role(role_arn, external_id)
    
    ec2_client = boto3.client(
        'ec2',
        region_name=region,
        aws_access_key_id=ak,
        aws_secret_access_key=sk,
        aws_session_token=token
    )
    
    resources = []
    paginator = ec2_client.get_paginator('describe_volumes')
    
    for page in paginator.paginate():
        for vol in page.get('Volumes', []):
            
            vol_id = vol['VolumeId']
            arn = f"arn:aws:ec2:{region}:{account_id}:volume/{vol_id}"
            
            tags = {tag['Key']: tag['Value'] for tag in vol.get('Tags', [])}
            
            attachments = vol.get('Attachments', [])
            is_attached = len(attachments) > 0
            attached_instances = [att['InstanceId'] for att in attachments]
            
            res = Resource(
                tenant_id=tenant_id,
                account_id=account_id,
                region=region,
                service="ebs",
                resource_type="volume",
                resource_id=vol_id,
                arn=arn,
                tags=tags,
                raw_metadata={
                    "State": vol['State'], # 'available' == unattached
                    "Size": vol['Size'],
                    "VolumeType": vol['VolumeType'],
                    "Iops": vol.get('Iops', 0),
                    "IsAttached": is_attached,
                    "AttachedInstanceIds": attached_instances,
                    "CreateTime": vol['CreateTime'].isoformat()
                }
            )
            
            resources.append(res)
            
    return resources
