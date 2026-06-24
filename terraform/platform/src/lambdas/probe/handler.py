import time
import json
import boto3
import logging
from typing import Dict, Any, List
from datetime import datetime, timezone
from botocore.exceptions import ClientError, WaiterError


logger = logging.getLogger()
logger.setLevel(logging.INFO)

class ExecutionEngine:
    def __init__(self, tenant_id: str, tenant_role_arn: str, region: str, sns_topic_arn: str, external_id: str):
        self.tenant_id = tenant_id
        self.region = region
        self.sns_topic_arn = sns_topic_arn
        
        sts_client = boto3.client('sts')
        try:
            assumed_role = sts_client.assume_role(
                RoleArn=tenant_role_arn,
                RoleSessionName=f"CloudOptixExec-{tenant_id}",
                ExternalId=external_id,
                DurationSeconds=900 
            )
            credentials = assumed_role['Credentials']

            tenant_session = {
                'aws_access_key_id': credentials['AccessKeyId'],
                'aws_secret_access_key': credentials['SecretAccessKey'],
                'aws_session_token': credentials['SessionToken'],
                'region_name': region
            }
            
            self.ec2 = boto3.client('ec2', **tenant_session)
            self.backup = boto3.client('backup', **tenant_session)
            self.cloudwatch = boto3.client('cloudwatch', **tenant_session)
            
            self.sns = boto3.client('sns', region_name=region)
        
        except ClientError as e:
            logger.critical(f"STS Authentication failed for Tenant {tenant_id}: {e}")
            raise Exception("Authentication Failure. Cannot execute actions.")
        
    
    def execute_rule_result(self, resource_id: str, resource_type: str, rule_result: Dict[str, Any])-> Dict[str, Any]:
        action = rule_result.get('action')
        tasks = rule_result.get('system_tasks', [])
        
        if action == 'TIER_3_IAC':
            logger.info(f"[{resource_id}] Tier 3 Action. IaC generation only. No execution required.")
            return {"status": "SUCCESS", "message": "IaC generated and queued for tenant approval."}
    
        if tasks:
            self._execute_system_tasks(tasks, tenant_role_arn=rule_result.get('backup_role_arn')) # type: ignore
            
        if action == 'TIER_1_STOP' and resource_type == 'instance':
            return self._handle_ec2_stop_with_rollback(resource_id)

        elif action == 'TIER_1_DELETE' and resource_type == 'volume':
            return self._handle_ebs_delete(resource_id)
        
        return {"status": "IGNORED", "message": "No actionable Tier 1 command recognized."}

    def _execute_system_tasks(self, tasks: List[Dict[str, Any]], tenant_role_arn: str):
        for task in tasks:
            if task.get('type') == 'START_BACKUP_JOB':
                res_arn = task.get('resource_id')
                vault = task.get('vault_name')
                
                try:
                    logger.info(f"Starting AWS Backup job for {res_arn} into {vault}")
                    response = self.backup.start_backup_job(
                        BackupVaultName=vault,
                        ResourceArn=res_arn,
                        IamRoleArn=tenant_role_arn
                    )
                    job_id = response.get('BackupJobId')
                    
                    timeout = time.time() + 60
                    
                    while time.time() < timeout:
                        job_status = self.backup.describe_backup_job(BackupJobId=job_id)
                        state = job_status.get('State')
                        if state in ['RUNNING', 'COMPLETED']:
                            break
                        elif state in ['FAILED', 'ABORTED']:
                            raise Exception(f"Backup job {job_id} failed with state: {state}")
                        time.sleep(5)
                    
                except ClientError as e:
                    logger.error(f"Backup task failed for {res_arn}: {e}")
                    raise Exception("Pre-flight safety task failed. Action aborted.")
        
    def _handle_ec2_stop_with_rollback(self, instance_id: str) -> Dict[str,Any]:
        logger.info(f"[{instance_id}] Initiating Tier 1 Stop.")
        
        action_timestamp = datetime.now(timezone.utc)
        
        try:
            self.ec2.stop_instances(InstanceIds=[instance_id])
            
            waiter = self.ec2.get_waiter('instance_stopped')
            waiter.wait(InstanceIds=[instance_id], WaiterConfig={'Delay': 15, 'MaxAttempts': 40})
            
            time.sleep(180)
            
            triggered_alarms =  self._probe_environment_health(action_timestamp)
            
            if triggered_alarms:
                logger.warning(f"[{instance_id}] PROBE FAILED. {len(triggered_alarms)} alarms triggered.")
                
                return self._trigger_rollback(
                    instance_id, 
                    reason=f"Environment instability detected. Triggered Alarms: {', '.join(triggered_alarms)}"
                )
            
            return {"status": "SUCCESS", "message": "Instance stopped safely. Environment is stable."}
        
        except WaiterError as we:
            logger.error(f"Instance {instance_id} failed to stop cleanly: {we}")
            return self._trigger_rollback(instance_id, "Instance state transition timeout.")
        
        except Exception as e:
            logger.error(f"Execution error for {instance_id}: {e}")
            return self._trigger_rollback(instance_id, f"Unexpected Execution Exception: {str(e)}")
        
    
    def _probe_environment_health(self, action_timestamp: datetime) -> List[str]:
        triggered_alarms = []
        paginator = self.cloudwatch.get_paginator('describe_alarms')
        
        
        try:
            for page in paginator.paginate(StateValue='ALARM'):
                for alarm in page.get('MetricAlarms', []):
                    updated_at = alarm.get('StateUpdatedTimestamp')
                    
                    if updated_at and updated_at >= action_timestamp:
                        triggered_alarms.append(alarm.get('AlarmName'))
        
        except ClientError as e:
            logger.error(f"Failed to query CloudWatch metrics: {e}")
            triggered_alarms.append("CLOUDWATCH_PROBE_FAILURE")
            
        return triggered_alarms
    
    
    def _handle_ebs_delete(self, volume_id: str) -> Dict[str, Any]:
        try:
            self.ec2.delete_volume(VolumeId=volume_id)
            return {"status": "SUCCESS", "message": "Orphaned volume deleted."}
        except ClientError as e:
            logger.error(f"Failed to delete volume {volume_id}: {e}")
            return {"status": "FAILED", "message": str(e)}
    
    def _trigger_rollback(self, instance_id: str, reason: str) -> Dict[str,Any]:
        logger.error(f"[{instance_id}] EXECUTING ROLLBACK. Reason: {reason}")
        
        try:
            self.ec2.start_instances(InstanceIds=[instance_id])
            
            waiter = self.ec2.get_waiter('instance_running')
            waiter.wait(InstanceIds=[instance_id], WaiterConfig={'Delay': 15, 'MaxAttempts': 40})
            
            alert_payload = {
                "event_type": "AUTONOMOUS_ROLLBACK",
                "severity": "CRITICAL",
                "tenant_id": self.tenant_id,
                "resource_id": instance_id,
                "action_attempted": "TIER_1_STOP",
                "rollback_reason": reason,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
            
            self.sns.publish(
                TopicArn=self.sns_topic_arn,
                Subject=f"CloudOptix Rollback: Tenant {self.tenant_id}",
                Message=json.dumps(alert_payload)
            )
            
            return {"status": "ROLLED_BACK", "message": f"Action reversed. {reason}"}

        except Exception as e:
            logger.critical(f"FATAL ROLLBACK FAILURE for {instance_id}: {e}")
            return {"status": "ROLLBACK_FAILED", "message": f"System state corrupted. Error: {e}"}
        
    
    
def lambda_handler(event, context):
    engine = ExecutionEngine(
        tenant_id=event['tenant_id'],
        tenant_role_arn=event['tenant_role_arn'],
        region=event['region'],
        sns_topic_arn=event['sns_topic_arn'],
        external_id=event.get('external_id', 'cloudoptix-ext-test-001')
    )
    
    return engine.execute_rule_result(
        resource_id=event['resource_id'],
        resource_type=event['resource_type'],
        rule_result=event['rule_result']
    )