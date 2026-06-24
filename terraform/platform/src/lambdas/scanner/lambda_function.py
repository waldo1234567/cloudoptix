import boto3
from typing import List
from .auth import assume_tenant_role
from rules.models import Resource

def discover(tenant_id: str, account_id: str, region: str, role_arn: str, external_id: str) -> List[Resource]:
    ak , sk, token = assume_tenant_role(role_arn, external_id)
    
    lambda_client = boto3.client(
        'lambda',
        region_name=region,
        aws_access_key_id=ak,
        aws_secret_access_key=sk,
        aws_session_token=token
    )
    
    resources = []
    paginator = lambda_client.get_paginator('list_functions')
    
    for page in paginator.paginate():
        for func in page.get('Functions', []):
            
            arn = func['FunctionArn']
            func_name = func['FunctionName']
            
            try:
                tag_response = lambda_client.list_tags(Resource=arn)
                tags = tag_response.get('Tags', {})
            except Exception as e:
                tags = {}
                print(e)
                
            res = Resource(
                tenant_id=tenant_id,
                account_id=account_id,
                region=region,
                service="lambda",
                resource_type="function",
                resource_id=func_name,
                arn=arn,
                tags=tags,
                raw_metadata={
                    "MemorySize": func['MemorySize'],
                    "Runtime": func.get('Runtime', 'provided'),
                    "State": func.get('State', 'Active'),
                    "LastModified": func['LastModified'],
                    "VpcConfig": func.get('VpcConfig', {})
                }
            )
            
            resources.append(res)
    
    return resources