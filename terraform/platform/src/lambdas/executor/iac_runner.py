import os
import uuid
import boto3
import logging
import json
from typing import Dict, Any
from datetime import datetime, timezone
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

class AutonomusIaCEngine:
    def __init__(self, tenant_id: str, tenant_role_arn: str, region: str):
        self.tenant_id = tenant_id
        self.tenant_role_arn = tenant_role_arn
        self.region = region
        
        self.s3 = boto3.client('s3')
        self.codebuild = boto3.client('codebuild')
        self.dynamodb = boto3.resource('dynamodb')
        
        self.config_bucket = os.environ.get('CONFIG_BUCKET', 'cloudoptix-tenant-configs-prod')
        self.state_bucket = os.environ.get('STATE_BUCKET', 'cloudoptix-tenant-tfstate-prod')
        self.builder_project = os.environ.get('CODEBUILD_PROJECT', 'CloudOptix-Terraform-Runner')
        self.history_table = self.dynamodb.Table(os.environ.get('HISTORY_TABLE', 'cloudoptix-execution-history')) # type: ignore
        
    
    def apply_autonomus_change(self, resource_id: str, new_hcl_content: str, reason: str, user_id: str) -> Dict[str, Any]:
        
        file_key = f"{self.tenant_id}/{resource_id}/main.tf"
        action_id = str(uuid.uuid4())
        
        try:
            put_response = self.s3.put_object(
                Bucket=self.config_bucket,
                Key=file_key,
                Body=new_hcl_content.encode('utf-8')
            )
            
            version_id = put_response.get('VersionId')
            
            if not version_id:
                raise Exception("S3 Object Versioning is not returning a VersionId. Architecture compromised.")
            
            build_id = self._trigger_codebuild(file_key, resource_id, action_id)
            
            self._log_version_history(
                action_id=action_id,
                resource_id=resource_id, 
                version_id=version_id, 
                build_id=build_id,
                action_type="APPLY",
                reason=reason,
                user_id=user_id,
                hcl_content=new_hcl_content
            )
            
            return {
                "status": "APPLYING", 
                "action_id": action_id,
                "build_id": build_id,
                "version_id": version_id,
                "message": "Autonomous Terraform deployment initiated."
            }
            
        except ClientError as e:
            print("failed to execute: " , e)
            logger.error(f"Failed to start autonomous apply for {resource_id}: {e}")
            return {"status": "FAILED", "message": str(e)}
    
    def revert_to_previous_version(self, resource_id: str, target_version_id: str, reason: str, user_id: str) -> Dict[str,Any]:
        file_key = f"{self.tenant_id}/{resource_id}/main.tf"
        action_id = str(uuid.uuid4())
        
        try:
            old_object = self.s3.get_object(
                Bucket=self.config_bucket,
                Key=file_key,
                VersionId=target_version_id
            )
            historical_hcl = old_object['Body'].read()
            
            put_response = self.s3.put_object(
                Bucket = self.config_bucket,
                Key = file_key,
                Body = historical_hcl
            )
            
            new_version_id = put_response.get('VersionId')
            
            build_id = self._trigger_codebuild(file_key, resource_id, action_id)
            
            self._log_version_history(
                action_id=action_id,
                resource_id=resource_id, 
                version_id=new_version_id, 
                build_id=build_id,
                action_type="REVERT",
                reason=f"Rollback to {target_version_id}: {reason}",
                user_id=user_id,
                hcl_content=historical_hcl.decode('utf-8')
            )
            
            return {
                "status": "REVERTING", 
                "action_id": action_id,
                "build_id": build_id,
                "message": f"Reverting infrastructure to historical state."
            }
        
        except ClientError as e:
            logger.error(f"Failed to trigger revert for {resource_id}: {e}")
            return {"status": "FAILED", "message": str(e)}
        
    
    def _trigger_codebuild(self, s3_config_key: str, resource_id: str, action_id: str) -> str:
        env_vars = [
            {'name': 'ACTION_ID', 'value': action_id},
            {'name': 'TENANT_ID', 'value': self.tenant_id},
            {'name': 'TENANT_ROLE_ARN', 'value': self.tenant_role_arn},
            {'name': 'RESOURCE_ID', 'value': resource_id},
            {'name': 'CONFIG_BUCKET', 'value': self.config_bucket},
            {'name': 'CONFIG_KEY', 'value': s3_config_key},
            {'name': 'STATE_BUCKET', 'value': self.state_bucket},
            {'name': 'AWS_REGION', 'value': self.region}
        ]
        
        response = self.codebuild.start_build(
            projectName=self.builder_project,
            environmentVariablesOverride=env_vars
        )
        
        return response['build']['id']
    

    def _log_version_history(self, action_id: str, resource_id: str, version_id: str, build_id: str, action_type: str, reason: str, user_id: str, hcl_content: str):
        timestamp = datetime.now(timezone.utc).isoformat()
        
        self.history_table.put_item(
            Item={
                'PK': f"TENANT#{self.tenant_id}",
                'SK': f"HISTORY#{resource_id}#{timestamp}",
                'ActionId': action_id,
                'ResourceId': resource_id,
                'ActionType': action_type,
                'VersionId': version_id,
                'CodeBuildId': build_id,
                'Reason': reason,
                'UserId': user_id,
                'Status': 'IN_PROGRESS', 
                'Timestamp': timestamp,
                'HclSnapshot': hcl_content
            }
        )

def lambda_handler(event, context):
    
    try:
        authorizer = event.get('requestContext', {}).get('authorizer', {})
        
        if 'jwt' in authorizer:
            claims = authorizer['jwt'].get('claims', {})
        else:
            claims = authorizer.get('claims', {})
        
        tenant_id =  claims.get('custom:tenant_id') or claims.get('sub') or claims.get('cognito:username')
        if not tenant_id:
            return {"statusCode": 401, "body": json.dumps({"error": "Unauthorized. Invalid JWT context."})}
    
    except Exception as e:
        return {"statusCode": 500, "body": json.dumps({"error": "Auth processing failure."})}
    
    try:
        
        body = json.loads(event.get('body', '{}'))
        
        engine = AutonomusIaCEngine(
            tenant_id = tenant_id,
            tenant_role_arn=body.get('tenant_role_arn', 'arn:aws:iam::123456789012:role/MockRole'),
            region=body.get('region', 'ap-northeast-1')
        )
        
        action = body.get('command')
        result = {}
        
        if action == "APPLY":
            result = engine.apply_autonomus_change(
                resource_id=body.get('resource_id'),
                new_hcl_content=body.get('hcl_content'),
                reason=body.get('reason', 'API Triggered'),
                user_id=tenant_id
            )    
            
        elif action == "REVERT":
            result = engine.revert_to_previous_version(
                resource_id=body.get('resource_id'),
                target_version_id=body.get('version_id'),
                reason=body.get('reason', 'API Triggered Revert'),
                user_id=tenant_id
            )
            
        else:
            return {
                "statusCode": 400,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"error": f"Unknown command: {action}"})
            }
        
        return {
            "statusCode": 200,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*"
            },
            "body": json.dumps(result)
        }
    
    except Exception as e:
        logger.error(f"Executor crashed: {str(e)}", exc_info=True)
        return {
            "statusCode": 500,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*"
            },
            "body": json.dumps({"error": str(e)})
        }
        