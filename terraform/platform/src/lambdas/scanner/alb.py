import boto3
from typing import List
from .auth import assume_tenant_role
from rules.models import Resource
def discover(tenant_id: str, account_id: str, region: str, role_arn: str, external_id: str) -> List[Resource]:
    ak, sk, token = assume_tenant_role(role_arn, external_id)
    
    elbv2_client = boto3.client(
        'elbv2',
        region_name=region,
        aws_access_key_id=ak,
        aws_secret_access_key=sk,
        aws_session_token=token
    )
    
    resources = []
    
    paginator = elbv2_client.get_paginator('describe_load_balancers')
    
    for page in paginator.paginate():
        for lb in page.get('LoadBalancers', []):
            
            arn = lb['LoadBalancerArn']
            lb_name = lb['LoadBalancerName']
            
            try:
                tag_response = elbv2_client.describe_tags(ResourceArns=[arn])
                tag_list = tag_response['TagDescriptions'][0].get('Tags', [])
                tags = {tag['Key']: tag['Value'] for tag in tag_list}
            except Exception as e:
                print(e)
                tags={}
    
            target_groups = []
            
            try:
                tg_response = elbv2_client.describe_target_groups(LoadBalancerArn = arn)
                target_groups = [tg['TargetGroupArn'] for tg in tg_response.get('TargetGroups', [])] 
            except Exception as e:
                print(e)
                pass   
            
            res = Resource(
                tenant_id=tenant_id,
                account_id=account_id,
                region=region,
                service="elbv2",
                resource_type="loadbalancer",
                resource_id=lb_name,
                arn=arn,
                tags=tags,
                raw_metadata={
                    "Type": lb['Type'], # application, network, or gateway
                    "Scheme": lb.get('Scheme', 'internet-facing'),
                    "State": lb['State']['Code'],
                    "VpcId": lb['VpcId'],
                    "TargetGroupArns": target_groups,
                    "CreatedTime": lb['CreatedTime'].isoformat()
                }
            )   

            resources.append(res)
    
    return resources