import boto3
from typing import List
from .auth import assume_tenant_role
from rules.models import Resource


def discover(tenant_id: str, account_id: str, region: str, role_arn: str, external_id: str) -> List[Resource]:
    ak, sk, token = assume_tenant_role(role_arn, external_id)
    
    elasticache_client = boto3.client(
        'elasticache',
        region_name=region,
        aws_access_key_id=ak,
        aws_secret_access_key=sk,
        aws_session_token=token
    )
    
    resources = []
    paginator = elasticache_client.get_paginator('describe_cache_clusters')
    
    
    for page in paginator.paginate():
        for cluster in page.get('CacheClusters', []):
            
            cluster_id = cluster['CacheClusterId']
            arn = f"arn:aws:elasticache:{region}:{account_id}:cluster:{cluster_id}"
            
            try:
                tag_response = elasticache_client.list_tags_for_resource(ResourceName=arn)
                tags = {tag['Key']: tag['Value'] for tag in tag_response.get('TagList', [])}
            except Exception:
                tags = {}
                
            res = Resource(
                tenant_id=tenant_id,
                account_id=account_id,
                region=region,
                service="elasticache",
                resource_type="cluster",
                resource_id=cluster_id,
                arn=arn,
                tags=tags,
                raw_metadata={
                    "CacheNodeType": cluster['CacheNodeType'],
                    "Engine": cluster['Engine'],
                    "EngineVersion": cluster['EngineVersion'],
                    "CacheClusterStatus": cluster['CacheClusterStatus'],
                    "NumCacheNodes": cluster['NumCacheNodes'],
                    "CacheClusterCreateTime": cluster['CacheClusterCreateTime'].isoformat()
                }
            )
            
            resources.append(res)
    
    return resources