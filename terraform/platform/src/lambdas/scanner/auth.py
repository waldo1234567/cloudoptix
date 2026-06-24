import boto3
from typing import Tuple

def assume_tenant_role(role_arn: str, external_id: str, session_name: str = "CloudOptixScanner")-> Tuple[str, str, str]:
    """
    Assumes the tenant's IAM role using the required ExternalID to prevent confused deputy attacks.
    Returns (access_key, secret_key, session_token).
    """
    sts_client = boto3.client('sts')
    response = sts_client.assume_role(
        RoleArn=role_arn,
        RoleSessionName=session_name,
        ExternalId=external_id,
        DurationSeconds=900
    )
    
    credentials = response['Credentials']
    return credentials['AccessKeyId'], credentials['SecretAccessKey'], credentials['SessionToken']