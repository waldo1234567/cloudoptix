import boto3
from botocore.exceptions import ClientError
from typing import List
from .auth import assume_tenant_role
from rules.models import Resource

def discover(tenant_id: str, account_id: str, region: str, role_arn: str, external_id: str) -> List[Resource]:
    ak, sk, token = assume_tenant_role(role_arn, external_id)
    
    s3_client = boto3.client(
        's3',
        region_name=region,
        aws_access_key_id=ak,
        aws_secret_access_key=sk,
        aws_session_token=token
    )
    
    resources = []
    
    response = s3_client.list_buckets()
    
    for bucket in response.get('Buckets', []):
        bucket_name = bucket['Name']
        arn = f"arn:aws:s3:::{bucket_name}"
        
        tags ={}
        
        try:
            tag_response = s3_client.get_bucket_tagging(Bucket=bucket_name)
            tags = {tag['Key']: tag['Value'] for tag in tag_response.get('TagSet', [])}
        except ClientError as e:
            if e.response['Error']['Code'] != 'NoSuchTagSet':
                pass # Ignore if no tags exist
            
        has_lifecycle = False
        try:
            s3_client.get_bucket_lifecycle_configuration(Bucket=bucket_name)
            has_lifecycle = True
        except ClientError as e:
            if e.response['Error']['Code'] != 'NoSuchLifecycleConfiguration':
                pass
        
        
        res = Resource(
            tenant_id=tenant_id,
            account_id=account_id,
            region=region, # Note: Actual bucket region might vary, but scanned via regional endpoint
            service="s3",
            resource_type="bucket",
            resource_id=bucket_name,
            arn=arn,
            tags=tags,
            raw_metadata={
                "CreationDate": bucket['CreationDate'].isoformat(),
                "HasLifecyclePolicy": has_lifecycle
            }
        )
        
        resources.append(res)
    
    return resources
