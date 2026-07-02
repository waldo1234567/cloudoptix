import boto3
from typing import List, Dict, Any
from .auth import assume_tenant_role
from rules.models import Resource

def discover(tenant_id: str, account_id: str, region:str, role_arn:str, external_id:str) -> List[Resource]:
    """
    Discovers EC2 instances in the target account/region and normalizes them.
    """
    ak, sk, token = assume_tenant_role(role_arn, external_id)
    ec2_client = boto3.client(
        'ec2',
        region_name = region,
        aws_access_key_id = ak,
        aws_secret_access_key = sk,
        aws_session_token = token
    )
    
    resources = []
    paginator = ec2_client.get_paginator('describe_instances')
    
    for page in paginator.paginate():
        for reservation in page.get('Reservations', []):
            for instance in reservation.get('Instances', []):
                # Terminated/shutting-down instances keep showing in describe_instances
                # for ~1h after deletion. Treat them as gone so drift is detected.
                if instance.get('State', {}).get('Name') in ('terminated', 'shutting-down'):
                    continue
                tags = {tag['Key']: tag['Value'] for tag in instance.get('Tags', [])}
                instance_id = instance['InstanceId']
                arn = f"arn:aws:ec2:{region}:{account_id}:instance/{instance_id}"
                
                res = Resource(
                    tenant_id=tenant_id,
                    account_id=account_id,
                    region = region,
                    service = "ec2",
                    resource_type="instance",
                    resource_id=instance_id,
                    arn=arn,
                    tags = tags,
                    raw_metadata={
                        "InstanceType": instance['InstanceType'],
                        "State": instance['State']['Name'],
                        "LaunchTime": instance['LaunchTime'].isoformat(),
                        "VpcId": instance.get('VpcId'),
                        "SubnetId": instance.get('SubnetId'),
                        "ImageId": instance.get('ImageId'),
                        "SecurityGroups" : [
                            {"GroupId": sg['GroupId'], "GroupName": sg['GroupName']}
                            for sg in instance.get('SecurityGroups', [])
                        ],
                        "BlockDeviceMappings":[
                            {"DeviceName": bdm['DeviceName'], "VolumeId": bdm.get('Ebs', {}).get('VolumeId')}
                            for bdm in instance.get('BlockDeviceMappings', [])
                        ]
                    }
                )
                
                resources.append(res)
                
    return resources