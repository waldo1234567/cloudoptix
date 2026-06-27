import os
import time
import json
import boto3
import logging
import urllib.request
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone
from botocore.exceptions import ClientError, WaiterError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

class ExecutionEngine:
    def __init__(self, tenant_id: str, tenant_role_arn: str, region: str, sns_topic_arn: str, external_id: str):
        self.tenant_id = tenant_id
        self.region = region
        self.sns_topic_arn = sns_topic_arn
        self.tenant_role_arn = tenant_role_arn
        
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
            self.rds = boto3.client('rds', **tenant_session)
            self.backup = boto3.client('backup', **tenant_session)
            self.cloudwatch = boto3.client('cloudwatch', **tenant_session)
            
            self.sns = boto3.client('sns', region_name=region)
        
        except ClientError as e:
            logger.critical(f"STS Authentication failed for Tenant {tenant_id}: {e}")
            raise Exception("Authentication Failure. Cannot execute actions.")
        
        self.ACTION_REGISTRY = {
            'TIER_1_STOP':    self._handle_tier_1_stop,
            'TIER_1_DELETE':  self._handle_tier_1_delete,
            'TIER_1_RELEASE': self._handle_tier_1_release,
            'TIER_3_IAC':     self._handle_tier_3_iac,
        }
        
    def execute_rule_result(self, resource_id: str, resource_type: str, rule_result: Dict[str, Any])-> Dict[str, Any]:
        action = rule_result.get('action')
        tasks = rule_result.get('system_tasks', [])
        
        logger.info(f"Probe action execution starting: action={action}, resource_id={resource_id}, resource_type={resource_type}")
        task_state = {}
        if tasks:
            task_state = self._execute_system_tasks(tasks, resource_id)
            if task_state.get('status') == 'FAILED': # type: ignore
                logger.error(f"Pre-flight task failed for {resource_id}. Aborting execution.")
                return task_state
        
        handler = self.ACTION_REGISTRY.get(action)
        if not handler:
            msg = f"No execution handler registered for action: {action}"
            logger.error(msg)
            return {"status": "FAILED", "message": msg}

        return handler(resource_id, resource_type, rule_result, task_state)

    def _poll_with_timeout(self, check_fn, pass_condition_fn, timeout_seconds: int = 300 , interval_seconds: int = 15, context:str = "") -> bool:
        attempts = timeout_seconds // interval_seconds
        for attempt in range(attempts):
            try:
                result = check_fn()
                if pass_condition_fn(result):
                    logger.info(f"Probe PASSED on attempt {attempt + 1}. Context: {context}")
                    return True
            except Exception as e:
                logger.warning(f"Probe attempt {attempt + 1} error: {e}. Context: {context}")
            time.sleep(interval_seconds)
        logger.error(f"Probe TIMEOUT after {timeout_seconds}s. Context: {context}")
        return False
    
    def _synthetic_http_probe(self, endpoint_url: str) -> bool:
        logger.info(f"Initiating synthetic HTTP probe: {endpoint_url}")
        for attempt in range(20):
            try:
                req = urllib.request.Request(endpoint_url, method="GET")
                with urllib.request.urlopen(req, timeout = 5) as response:
                    if response.status in [200, 201, 202]:
                        logger.info("Probe SUCCESS.")
                        return True
            
            except Exception as e:
                logger.warning(f"Probe attempt {attempt + 1} failed: {e}")
            time.sleep(15)
        logger.error("Probe TIMEOUT.")
        return False
    
    def _execute_system_tasks(self, tasks: List[Dict[str, Any]], resource_id: str) -> Dict[str, Any]:
        state = {"status": "SUCCESS"}
        for task in tasks:
            task_type = task.get('type')
            
            if task_type in ['START_BACKUP_JOB', 'CREATE_SNAPSHOT']:
                vol_id = task.get('volume_id', resource_id)
                try:
                    logger.info(f"Creating pre-flight snapshot for volume {vol_id}")
                    snap = self.ec2.create_snapshot(VolumeId=vol_id, Description=f"CloudOptix Auto-Backup {vol_id}")
                    snap_id = snap['SnapshotId']

                    waiter = self.ec2.get_waiter('snapshot_completed')
                    waiter.wait(SnapshotIds=[snap_id], WaiterConfig={'Delay': 15, 'MaxAttempts': 40})
                    
                    state['snapshot_id'] = snap_id
                    logger.info(f"Snapshot {snap_id} verified complete.")
                    
                except Exception as e:
                    logger.error(f"Pre-flight snapshot failed: {e}")
                    return {"status": "FAILED", "message": str(e)}
        return state
    
    def _handle_tier_3_iac(self, resource_id: str, resource_type: str, rule_result: Dict, task_state: Dict) -> Dict:
        hcl_diff = rule_result.get('terraform_hcl_diff', 'No HCL provided.')
        reasoning = rule_result.get('reasoning', 'No reasoning provided.')
        savings = rule_result.get('estimated_monthly_savings', 0.0)
        
        logger.info(f"\n=====================================")
        logger.info(f"GENERATED IAC FOR {resource_id}")
        logger.info(f"Reason: {reasoning}")
        logger.info(f"Savings: ${savings}/mo")
        logger.info(f"{hcl_diff}")
        logger.info(f"=====================================\n")
        
        try:
            dynamodb = boto3.resource('dynamodb', region_name=self.region)
            table = dynamodb.Table(os.environ.get('DYNAMODB_TABLE_NAME', 'cloudoptix-core-table')) # type: ignore
            table.put_item(
                Item={
                    'PK': f"TENANT#{self.tenant_id}",
                    'SK': f"FINDING#{resource_id}",
                    'Type': 'Recommendation',
                    'ResourceId': resource_id,
                    'ResourceType': resource_type,
                    'Action': 'TIER_3_IAC',
                    'Reasoning': reasoning,
                    'EstimatedMonthlySavings': str(savings), 
                    'TerraformHCL': hcl_diff,
                    'Status': 'PENDING_APPROVAL',
                    'Timestamp': datetime.now(timezone.utc).isoformat()
                }
            )
            return {"status": "SUCCESS", "message": "IaC generated and saved to DynamoDB database."}
        except Exception as e:
            logger.error(f"Failed to save finding to DynamoDB: {e}")
            return {"status": "FAILED", "message": f"Failed to save finding: {e}"}
    
    def _handle_tier_1_delete(self, resource_id: str, resource_type: str, rule_result: Dict, task_state: Dict) -> Dict:
        if resource_type == 'volume':
            return self._delete_ebs_volume(resource_id, task_state)
        elif resource_type == 'natgateway':
            return self._delete_nat_gateway(resource_id)
        else:
            return {"status": "FAILED", "message": f"TIER_1_DELETE not supported for resource_type={resource_type}."}
    
    def _handle_tier_1_stop(self, resource_id: str, resource_type: str, rule_result: Dict, task_state: Dict) -> Dict:
        if resource_type != 'instance': return {"status": "FAILED", "message": "Stop requires instance."}
        logger.info(f"[{resource_id}] Initiating TIER_1_STOP.")
        try:
            self.ec2.stop_instances(InstanceIds=[resource_id])
            
            waiter = self.ec2.get_waiter('instance_stopped')
            waiter.wait(InstanceIds=[resource_id], WaiterConfig={'Delay': 15, 'MaxAttempts': 20})
            
            logger.info(f"[{resource_id}] Instance stopped successfully.")
            return {"status": "STOPPED", "message": f"Instance {resource_id} stopped."}

        except WaiterError:
            return self._rollback_ec2_stop(resource_id, rule_result.get('validation_endpoint'), "Instance failed to reach STOPPED state within 5 minutes.")
        except Exception as e:
            return self._rollback_ec2_stop(resource_id, rule_result.get('validation_endpoint'), str(e))
        
    def _rollback_ec2_stop(self, instance_id: str, validation_url: Optional[str], reason: str) -> Dict[str, Any]:
        logger.warning(f"ROLLBACK EC2: {instance_id}. Reason: {reason}")
        try:
            self.ec2.start_instances(InstanceIds=[instance_id])
            waiter = self.ec2.get_waiter('instance_running')
            waiter.wait(InstanceIds=[instance_id], WaiterConfig={'Delay': 15, 'MaxAttempts': 40})
            
            if validation_url:
                if not self._synthetic_http_probe(validation_url):
                    self._trigger_sns_escalation(
                        instance_id, "ROLLBACK_DEGRADED",
                        f"Instance restarted but HTTP probe failed. Original stop reason: {reason}"
                    )
                    return {"status": "ROLLBACK_DEGRADED", "message": "Instance running but application health check failed."}
            
            self._trigger_sns_escalation(instance_id, "TIER_1_STOP", reason)
            return {"status": "ROLLED_BACK", "message": f"Reversed. {reason}"}
        except Exception as e:
            return {"status": "ROLLBACK_FAILED", "message": str(e)}
    
    def _delete_ebs_volume(self, volume_id: str, task_state: Dict) -> Dict[str, Any]:
        snapshot_id = task_state.get('snapshot_id')
        if not snapshot_id:
            return {"status": "FAILED", "message": "Pre-flight snapshot not verified. Aborting delete to protect data."}
 
        try:
            self.ec2.delete_volume(VolumeId=volume_id)
            logger.info(f"[{volume_id}] Volume deleted. Backup: {snapshot_id}.")
            return {"status": "DELETED", "message": f"Volume {volume_id} deleted. Backup verified in snapshot {snapshot_id}."}
        except ClientError as e:
            self._trigger_sns_escalation(
                volume_id, "TIER_1_DELETE_FAILED",
                f"Delete failed — volume still intact. Backup available at {snapshot_id}. Error: {str(e)}"
            )
            return {"status": "ESCALATED", "message": f"Delete failed. Volume still intact. Backup at {snapshot_id}."}
    
    def _delete_nat_gateway(self, nat_id: str) -> Dict[str, Any]:
        try:
            self.ec2.delete_nat_gateway(NatGatewayId=nat_id)
            logger.info(f"[{nat_id}] Delete request issued. Polling for confirmation.")
            
            passed = self._poll_with_timeout(
                check_fn=lambda: self.ec2.describe_nat_gateways(NatGatewayIds=[nat_id])['NatGateways'][0]['State'],
                pass_condition_fn=lambda state: state == 'deleted',
                timeout_seconds=300,
                context=f"NAT Gateway {nat_id} deletion"
            )
            
            if passed:
                logger.info(f"[{nat_id}] NAT Gateway confirmed deleted.")
                return {"status": "DELETED", "message": f"NAT Gateway {nat_id} deleted. Saving ~$32.50/month."}
            else:
                self._trigger_sns_escalation(
                    nat_id, "NAT_DELETE_TIMEOUT",
                    "NAT Gateway delete issued but did not confirm 'deleted' state within 300s. Verify manually."
                )
                return {"status": "ESCALATED", "message": "Delete issued but state confirmation timed out. Manual verification required."}
        
        except ClientError as e:
            self._trigger_sns_escalation(nat_id, "NAT_DELETE_FAILED", str(e))
            return {"status": "FAILED", "message": str(e)}
        
    def _handle_tier_1_release(self, resource_id: str, resource_type: str, rule_result: Dict, task_state: Dict) -> Dict:
        if resource_type != 'eip':
            return {"status": "FAILED", "message": f"TIER_1_RELEASE requires resource_type=eip, got {resource_type}."}
        
        try:
            self.ec2.release_address(AllocationId = resource_id)
            logger.info(f"[{resource_id}] EIP released successfully.")
            return {"status": "RELEASED", "message": f"EIP {resource_id} released. No rollback possible — action is irreversible."}

        except ClientError as e:
            error_code = e.response['Error']['Code']
            if error_code == 'AuthFailure':
                return {"status": "FAILED", "message": "Insufficient permissions to release EIP."}
            if error_code ==  'InvalidAllocationID.NotFound':
                return {"status": "SKIPPED", "message": "EIP no longer exists — may have been released manually."}
            self._trigger_sns_escalation(resource_id, "EIP_RELEASE_FAILED", str(e))
            return {"status": "FAILED", "message": str(e)}
    
    def _handle_rds_stop(self, db_instance_id: str, rule_result: Dict) -> Dict[str,Any]:
        logger.info(f"[{db_instance_id}] Initiating RDS stop.")
        
        try:
            self.rds.stop_db_instance(DBInstanceIdentifier=db_instance_id)
            
            passed = self._poll_with_timeout(
                check_fn=lambda: self.rds.describe_db_instances(DBInstanceIdentifier=db_instance_id)['DBInstances'][0]['DBInstanceStatus'],
                pass_condition_fn=lambda status: status == 'stopped',
                timeout_seconds=600,
                context=f"RDS stop {db_instance_id}"
            )
            
            if passed:
                logger.info(f"[{db_instance_id}] RDS instance stopped successfully.")
                return {"status": "STOPPED", "message": f"RDS instance {db_instance_id} stopped and confirmed."}
            else:
                return self._rollback_rds_stop(db_instance_id, "RDS instance failed to reach stopped state within 10 minutes.")
        except ClientError as e:
            return self._rollback_rds_stop(db_instance_id, str(e))
    
    def _rollback_rds_stop(self, db_instance_id: str, reason: str) -> Dict[str,Any]:
        logger.warning(f"[{db_instance_id}] ROLLBACK RDS STOP. Reason: {reason}")
        
        try:
            self.rds.start_db_instance(DBInstanceIdentifier=db_instance_id)
            
            self._poll_with_timeout(
                check_fn=lambda: self.rds.describe_db_instances(DBInstanceIdentifier=db_instance_id)['DBInstances'][0]['DBInstanceStatus'],
                pass_condition_fn=lambda status: status == 'available',
                timeout_seconds=600,
                context=f"RDS rollback start {db_instance_id}"
            )
            
            passed = self._poll_with_timeout(
                 check_fn=lambda: self.cloudwatch.get_metric_statistics(
                    Namespace='AWS/RDS',
                    MetricName='DatabaseConnections',
                    Dimensions=[{'Name': 'DBInstanceIdentifier', 'Value': db_instance_id}],
                    StartTime=datetime.now(timezone.utc).__class__.utcnow().__class__.utcnow(),
                    EndTime=datetime.now(timezone.utc),
                    Period=60,
                    Statistics=['Maximum']
                ).get('Datapoints', []),
                pass_condition_fn=lambda points: any(p.get('Maximum', 0) > 0 for p in points),
                timeout_seconds=600,
                context=f"RDS connection probe {db_instance_id}"
            )

            if not passed:
                self._trigger_sns_escalation(
                    db_instance_id, "RDS_ROLLBACK_DEGRADED",
                    f"RDS restarted but no connections re-established within 10 minutes. Original reason: {reason}"
                )
                return {"status": "ROLLBACK_DEGRADED", "message": "RDS running but no connections confirmed."}

            self._trigger_sns_escalation(db_instance_id, "RDS_STOP_ROLLED_BACK", reason)
            return {"status": "ROLLED_BACK", "message": f"RDS stop reversed. Reason: {reason}"}
        
        except Exception as e:
            self._trigger_sns_escalation(db_instance_id, "RDS_ROLLBACK_FAILED", f"CRITICAL: {e}")
            return {"status": "ROLLBACK_FAILED", "message": str(e)}
    
    
    
    def _trigger_sns_escalation(self, resource_id: str, event_type: str, reason: str):
        payload = {
            "event":       event_type,
            "severity":    "CRITICAL",
            "tenant_id":   self.tenant_id,
            "resource_id": resource_id,
            "reason":      reason,
            "timestamp":   datetime.now(timezone.utc).isoformat()
        }
        try:
            self.sns.publish(
                TopicArn=self.sns_topic_arn,
                Subject=f"CloudOptix Escalation: {event_type}",
                Message=json.dumps(payload)
            )
        except Exception as e:
             logger.critical(f"SNS escalation FAILED for {resource_id}: {e}. Original event: {event_type} — {reason}")

def _route_tier_1_stop(engine: 'ExecutionEngine', resource_id: str, resource_type: str, rule_result: Dict, task_state: Dict) -> Dict:
    if resource_type == 'instance':
        return engine._handle_tier_1_stop(resource_id, resource_type, rule_result, task_state)
    elif resource_type == 'db-instance':
        return engine._handle_rds_stop(resource_id, rule_result)
    else:
        return {"status": "FAILED", "message": f"TIER_1_STOP not supported for resource_type={resource_type}."}

    
def lambda_handler(event, context):
    logger.info(f"Probe lambda invoked with event: {event}")
    records = event.get("Records", [])
    if not records:
        raise ValueError("No SQS record found")
    
    record = records[0]
    logger.info(f"Probe received SQS record: {record}")
    
    try:
        body = json.loads(record["body"])
        logger.info(f"Probe parsed message body: {body}")
        engine = ExecutionEngine(
            tenant_id=body['tenant_id'],
            tenant_role_arn=body['tenant_role_arn'],
            region=body['region'],
            sns_topic_arn=body['sns_topic_arn'],
            external_id=body.get('external_id', 'cloudoptix-ext-test-001')
        )
        
        resource_id   = body['resource_id']
        resource_type = body['resource_type']
        rule_result   = body.get('rule_result', {})
        
        logger.info(f"Probe executing resource_id={resource_id}, resource_type={resource_type}")
        logger.info(f"Probe rule_result payload: {rule_result}")
        engine.ACTION_REGISTRY['TIER_1_STOP'] = lambda rid, rtype, rr, ts: _route_tier_1_stop(engine, rid, rtype, rr, ts) # type: ignore
 
        result = engine.execute_rule_result(resource_id, resource_type, rule_result)
        logger.info(f"Probe result: {result}")
        if isinstance(result, dict):
            result.setdefault("terraform_hcl_diff", rule_result.get("terraform_hcl_diff"))
        return result
    except Exception as e:
        logger.error(f"Probe Lambda crashed: {e}", exc_info=True)
        return {"status": "ERROR", "message": str(e)}