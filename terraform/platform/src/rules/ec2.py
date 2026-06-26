from typing import Dict, Any
from .models import Action, Confidence, RuleResult
from src.lambdas.metrics.classfier import WorkloadPattern

DOWNSIZE_MAP = {
    't3.medium': 't3.small', 't3.large': 't3.medium', 't3.xlarge': 't3.large',
    'm5.large': 'm5.medium', 'm5.xlarge': 'm5.large', 'm5.2xlarge': 'm5.xlarge',
    'c5.large': 'c5.medium', 'c5.xlarge': 'c5.large',
    'r5.large': 'r5.medium', 'r5.xlarge': 'r5.large',
}

EC2_MONTHLY_PRICING = {
    't3.small': 15.18, 't3.medium': 30.36, 't3.large': 60.73,
    'm5.large': 70.08, 'm5.medium': 35.04,
    'c5.large': 62.05, 'c5.medium': 31.02,
    'r5.large': 91.98, 'r5.medium': 45.99
}

def evaluate(resource: Dict[str, Any], metrics: Dict[str, Any],pattern: WorkloadPattern) -> RuleResult:
    meta = resource.get('RawMetadata', {})
    state = meta.get('State', 'unknown')
    instance_type = meta.get('InstanceType', '')
    res_id = resource.get('SK', '').split('RESOURCE#')[-1]
    
    if state != 'running':
        return RuleResult(Action.IGNORE, Confidence.HIGH, f"Instance state is '{state}'.", "SAFE", 0.0)
    
    
    status_failed = metrics.get('StatusCheckFailed', {}).get('Maximum', 1.0)
    
    if status_failed > 0:
        return RuleResult(
            Action.IGNORE, Confidence.HIGH, 
            "Instance reported failed status checks in the last 14 days.", 
            "HIGH - Host is unhealthy or AWS is performing maintenance.", 0.0
        )
        
    if instance_type.startswith('t'):
        credit_balance = metrics.get('CPUCreditBalance', {}).get('Average', 100)
        if credit_balance < 50:
            return RuleResult(
                Action.IGNORE, Confidence.HIGH, 
                f"Burstable instance is CPU-throttled (Avg Credit Balance: {credit_balance:.1f}).", 
                "HIGH - Modification will cause performance outage.", 0.0
            )
    
    current_cost = EC2_MONTHLY_PRICING.get(instance_type, 0.0)
    
    if pattern == WorkloadPattern.ABANDONED:
        return RuleResult(
            action=Action.TIER_1_STOP,
            confidence=Confidence.HIGH,
            reasoning="Instance is mathematically abandoned. Executor will Stop the instance and monitor for health check failures.",
            blast_radius_assessment="LOW - Resource is isolated and inactive. Rollback probe attached.",
            estimated_monthly_savings=current_cost
        )
    
    if pattern == WorkloadPattern.SPIKY:
        hcl_diff = f"""
# Architectural Migration: Static Instance -> Target-Tracked ASG
resource "aws_launch_template" "{res_id}_template" {{
  name_prefix   = "{res_id}-"
  instance_type = "{instance_type}"
  # AMI and Security Groups must be mapped from the original instance state
}}

resource "aws_autoscaling_group" "{res_id}_asg" {{
  desired_capacity    = 1
  max_size           = 3
  min_size           = 1
  launch_template {{
    id      = aws_launch_template.{res_id}_template.id
    version = "$Latest"
  }}
}}

resource "aws_autoscaling_policy" "{res_id}_tracking" {{
  name                   = "{res_id}-cpu-tracking"
  autoscaling_group_name = aws_autoscaling_group.{res_id}_asg.name
  policy_type            = "TargetTrackingScaling"
  target_tracking_configuration {{
    predefined_metric_specification {{
      predefined_metric_type = "ASGAverageCPUUtilization"
    }}
    target_value = 50.0
  }}
}}
        """
        
        return RuleResult(
            action=Action.TIER_3_IAC,
            confidence=Confidence.MEDIUM,
            reasoning="Workload exhibits extreme variance. Migrating to an ASG absorbs spikes while saving money during baseline.",
            blast_radius_assessment="MEDIUM - Requires architectural refactor and AMI creation.",
            estimated_monthly_savings=current_cost * 0.40,
            terraform_hcl_diff=hcl_diff.strip()
        )
    
    if pattern == WorkloadPattern.SCHEDULED:
        ami_id          = meta.get('ImageId', '<ami-id-from: terraform state list | grep ' + res_id + '>')
        subnet_id       = meta.get('SubnetId', '')
        security_groups = meta.get('SecurityGroups', [])
        sg_ids          = [sg['GroupId'] for sg in security_groups]
        sg_ids_str      = ', '.join(f'"{g}"' for g in sg_ids) if sg_ids else '# No security groups found in scanner'
        
        
        hcl_diff = f"""
# Architectural Migration: Native AWS ASG Scheduling
resource "aws_ami_from_instance" "{res_id}_backup" {{
  name               = "{res_id}-scheduled-baseline"
  source_instance_id = "{res_id}"
}}

resource "aws_launch_template" "{res_id}_template" {{
    name_prefix   = "{res_id}-"
    image_id      = "{ami_id}"
    instance_type = "{instance_type}"
    vpc_security_group_ids = [{sg_ids_str}]
}}

resource "aws_autoscaling_group" "{res_id}_asg" {{
  vpc_zone_identifier = ["{subnet_id}"]
  desired_capacity    = 1
  max_size            = 1
  min_size            = 0
  
  launch_template {{
    id      = aws_launch_template.{res_id}_template.id
    version = "$Latest"
  }}
}}
resource "aws_autoscaling_schedule" "{res_id}_scale_down" {{
  scheduled_action_name  = "scale-down-evening"
  min_size               = 0
  max_size               = 0
  desired_capacity       = 0
  recurrence             = "0 19 * * *"
  autoscaling_group_name = aws_autoscaling_group.{res_id}_asg.name
}}

resource "aws_autoscaling_schedule" "{res_id}_scale_up" {{
  scheduled_action_name  = "scale-up-morning"
  min_size               = 1
  max_size               = 1
  desired_capacity       = 1
  recurrence             = "0 8 * * *"
  autoscaling_group_name = aws_autoscaling_group.{res_id}_asg.name
}}
        """
        return RuleResult(
            action=Action.TIER_3_IAC,
            confidence=Confidence.HIGH,
            reasoning="Workload maps perfectly to business hours. Wrapping in an ASG with cron-based scaling automates shutdowns safely.",
            blast_radius_assessment="LOW - Wrapping a single instance in an ASG requires minimal configuration.",
            estimated_monthly_savings=current_cost * 0.65, # Saving ~65% by turning off nights and weekends
            terraform_hcl_diff=hcl_diff.strip(),
            system_tasks=[]
        )
    
    if pattern == WorkloadPattern.ALWAYS_ON_IDLE:
        target_type = DOWNSIZE_MAP.get(instance_type)
        if target_type is None:
            return RuleResult(
                action=Action.IGNORE,
                confidence=Confidence.LOW,
                reasoning=f"No safe downsize path mapped for {instance_type}.",
                blast_radius_assessment="SAFE",
                estimated_monthly_savings=0.0
            )        
        
        target_cost = EC2_MONTHLY_PRICING.get(target_type, 0.0)
        savings = current_cost - target_cost if current_cost and target_cost else 0.0
        
        hcl_diff = f"""
        # Architectural Migration: Static Downsize
resource "aws_instance" "{res_id}" {{
-  instance_type = "{instance_type}"
+  instance_type = "{target_type}"
}}
        """
        return RuleResult(
            action = Action.TIER_3_IAC,
            confidence=Confidence.HIGH,
            reasoning=f"Workload is consistently flat and over-provisioned. Downsizing to {target_type}.",
            blast_radius_assessment="MEDIUM - Requires instance reboot.",
            estimated_monthly_savings=savings,
            terraform_hcl_diff=hcl_diff.strip()
        )      
    
    #Catch All
    return RuleResult(
        Action.IGNORE, Confidence.HIGH, "Resource is active and appropriately provisioned.", "SAFE", 0.0
    )