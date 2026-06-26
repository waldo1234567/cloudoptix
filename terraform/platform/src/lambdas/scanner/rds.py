import boto3
from typing import List
from .auth import assume_tenant_role
from rules.models import Resource

def discover(tenant_id: str, account_id: str, region: str, role_arn: str, external_id: str) -> List[Resource]:
    ak,sk,token = assume_tenant_role(role_arn, external_id)
    
    rds_client = boto3.client(
        'rds',
        region_name = region,
        aws_access_key_id = ak,
        aws_secret_access_key = sk,
        aws_session_token = token
    )
    
    resources = []
    paginator = rds_client.get_paginator('describe_db_instances')
    
    for page in paginator.paginate():
        for db in page.get('DBInstances', []):
            
            arn = db['DBInstanceArn']
            db_id = db['DBInstanceIdentifier']
            
            tag_response = rds_client.list_tags_for_resource(ResourceName = arn)
            
            tags = {tag['Key']: tag['Value'] for tag in tag_response.get('TagList', [])}
            
            res = Resource(
                tenant_id=tenant_id,
                account_id=account_id,
                region=region,
                service="rds",
                resource_type="db-instance",
                resource_id=db_id,
                arn=arn,
                tags=tags,
                raw_metadata={
                    "DBInstanceClass": db['DBInstanceClass'],
                    "Engine": db['Engine'],
                    "DBInstanceStatus": db['DBInstanceStatus'],
                    "MultiAZ": db['MultiAZ'], 
                    "PubliclyAccessible": db['PubliclyAccessible'],
                    "VpcSecurityGroups": [sg['VpcSecurityGroupId'] for sg in db.get('VpcSecurityGroups', [])],
                    "DBSubnetGroupName":  db.get('DBSubnetGroup', {}).get('DBSubnetGroupName'),
                    "AvailabilityZone": db.get('AvailabilityZone'),
                    "AllocatedStorage": db.get('AllocatedStorage'),
                    "StorageType": db.get('StorageType')
                }
            )
            
            resources.append(res)
            
    return resources







