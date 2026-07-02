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
    paginator = ec2_client.get_paginator('describe_nat_gateways')
    
    
    for page in paginator.paginate():
        for nat in page.get('NatGateways', []):
            # Deleted NAT gateways linger in describe_nat_gateways (~1h). Skip them.
            if nat.get('State') in ('deleted', 'deleting', 'failed'):
                continue

            nat_id = nat['NatGatewayId']
            arn = f"arn:aws:ec2:{region}:{account_id}:natgateway/{nat_id}"
            
            tags = {tag['Key']: tag['Value'] for tag in nat.get('Tags', [])}
            eip_allocation_id = None
            for addr in nat.get('NatGatewayAddresses', []):
                if addr.get('AllocationId'):
                    eip_allocation_id = addr['AllocationId']
                    break
            res = Resource(
                tenant_id=tenant_id,
                account_id=account_id,
                region=region,
                service="ec2",
                resource_type="natgateway",
                resource_id=nat_id,
                arn=arn,
                tags=tags,
                raw_metadata={
                    "VpcId": nat['VpcId'],
                    "SubnetId": nat['SubnetId'],
                    "State": nat['State'],
                    "ConnectivityType": nat.get('ConnectivityType', 'public'),
                    "CreateTime": nat['CreateTime'].isoformat(),
                    "EipAllocationId": eip_allocation_id
                }
            )
            
            resources.append(res)
        
    return resources

