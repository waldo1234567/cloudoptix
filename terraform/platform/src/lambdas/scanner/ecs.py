import boto3
from typing import List
from .auth import assume_tenant_role
from rules.models import Resource

def discover(tenant_id: str, account_id: str, region: str, role_arn: str, external_id: str) -> List[Resource]:
    ak, sk, token = assume_tenant_role(role_arn, external_id)
    
    ecs_client = boto3.client(
        'ecs',
        region_name=region,
        aws_access_key_id=ak,
        aws_secret_access_key=sk,
        aws_session_token=token
    )
    
    resources = []
    
    
    cluster_paginator = ecs_client.get_paginator('list_clusters')
    for cluster_page in cluster_paginator.paginate():
        cluster_arns = cluster_page.get('clusterArns', [])
        
        for cluster_arn in cluster_arns:
            
            service_paginator = ecs_client.get_paginator('list_services')
            for service_page in service_paginator.paginate(cluster=cluster_arn):
                service_arns = service_page.get('serviceArns', [])
                
                if not service_arns:
                    continue
                    
                for i in range(0, len(service_arns), 10):
                    chunk = service_arns[i:i+10]
                    
                    desc_response = ecs_client.describe_services(
                        cluster=cluster_arn,
                        services=chunk,
                        include=['TAGS']
                    )
                    
                    for svc in desc_response.get('services', []):
                        svc_arn = svc['serviceArn']
                        svc_name = svc['serviceName']
                        
                        tags = {tag['Key']: tag['Value'] for tag in svc.get('tags', [])}
                        
                        target_groups = [lb.get('targetGroupArn') for lb in svc.get('loadBalancers', []) if 'targetGroupArn' in lb]
                        
                        res = Resource(
                            tenant_id=tenant_id,
                            account_id=account_id,
                            region=region,
                            service="ecs",
                            resource_type="service",
                            resource_id=f"{cluster_arn.split('/')[-1]}/{svc_name}",
                            arn=svc_arn,
                            tags=tags,
                            raw_metadata={
                                "Status": svc['status'],
                                "RunningCount": svc['runningCount'],
                                "DesiredCount": svc['desiredCount'],
                                "LaunchType": svc.get('launchType', 'UNKNOWN'),
                                "TaskDefinition": svc['taskDefinition'],
                                "TargetGroupArns": target_groups,
                                "ClusterArn": cluster_arn
                            }
                        )
                        resources.append(res)
                        
    return resources
    