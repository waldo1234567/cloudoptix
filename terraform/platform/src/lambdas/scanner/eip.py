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
    
    response = ec2_client.describe_addresses()
    
    for address in response.get('Addresses', []):
        
        allocation_id = address.get('AllocationId', address['PublicIp'])
        
        arn = f"arn:aws:ec2:{region}:{account_id}:eip/{allocation_id}" #pseudo-arn for uniformity

        tags = {tag['Key']: tag['Value'] for tag in address.get('Tags', [])}
        
        is_attached = 'AssociationId' in address or 'InstanceId' in address
        
        res = Resource(
            tenant_id=tenant_id,
            account_id=account_id,
            region=region,
            service="ec2",
            resource_type="eip",
            resource_id=allocation_id,
            arn=arn,
            tags=tags,
            raw_metadata={
                "PublicIp": address['PublicIp'],
                "Domain": address.get('Domain', 'vpc'),
                "IsAttached": is_attached,
                "AssociatedInstanceId": address.get('InstanceId'),
                "NetworkInterfaceId": address.get('NetworkInterfaceId')
            }
        )    
        resources.append(res)
        
    return resources    
        