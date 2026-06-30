import json
from typing import Dict, Any
from .models import Action, Confidence, RuleResult, HCLEdit
from lambdas.metrics.classfier import WorkloadPattern

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

def evaluate(resource: Dict[str, Any], metrics: Dict[str, Any], pattern: WorkloadPattern) -> RuleResult:
    meta = resource.get('RawMetadata', {})
    res_id = resource.get('SK', '').split('RESOURCE#')[-1]
    instance_type = meta.get('InstanceType')
    current_cost = EC2_MONTHLY_PRICING.get(instance_type, 0.0)

    ami_id = meta.get('ImageId', 'ami-REQUIRED')
    subnet_id = meta.get('SubnetId', 'subnet-REQUIRED')
    sg_ids = json.dumps([sg.get('GroupId') for sg in meta.get('SecurityGroups', [])])

    if pattern == WorkloadPattern.ABANDONED:
        return RuleResult(
            action=Action.TIER_1_STOP,
            confidence=Confidence.HIGH,
            reasoning="Instance is mathematically abandoned. Stop the instance and monitor for health check failures.",
            blast_radius_assessment="LOW - Resource is isolated and inactive. Rollback probe attached.",
            estimated_monthly_savings=current_cost,
            hcl_edits=[HCLEdit(
                edit_type="update_attribute",
                resource_address="__TF_ADDRESS__",
                attribute_path="instance_state",
                old_value="running",
                new_value="stopped",
            )],
        )

    if pattern == WorkloadPattern.SPIKY:
        hcl_diff = f"""
# Architectural Migration: Static Instance -> Target-Tracked ASG
resource "aws_launch_template" "__TF_ALIAS___tmpl" {{
  name_prefix   = "spiky-tmpl-"
  image_id      = "{ami_id}"
  instance_type = "{instance_type if instance_type else 't3.micro'}"
  vpc_security_group_ids = {sg_ids}
}}

resource "aws_autoscaling_group" "__TF_ALIAS___asg" {{
  vpc_zone_identifier = ["{subnet_id}"]
  desired_capacity    = 1
  max_size           = 3
  min_size           = 1
  launch_template {{
    id      = aws_launch_template.__TF_ALIAS___tmpl.id
    version = "$Latest"
  }}
}}

resource "aws_autoscaling_policy" "__TF_ALIAS___cpu_policy" {{
  name                   = "cpu-target-tracking"
  autoscaling_group_name = aws_autoscaling_group.__TF_ALIAS___asg.name
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
            estimated_monthly_savings=0.0,
            hcl_edits=[HCLEdit(
                edit_type="replace_resource",
                resource_address="__TF_ADDRESS__",
                full_resource_hcl=hcl_diff.strip(),
            )],
        )

    if pattern == WorkloadPattern.SCHEDULED:
        ami_id          = meta.get('ImageId', 'ami-REQUIRED')
        subnet_id       = meta.get('SubnetId', 'subnet-REQUIRED')
        security_groups = meta.get('SecurityGroups', [])
        sg_ids          = [sg['GroupId'] for sg in security_groups]
        sg_ids_str      = ', '.join(f'"{g}"' for g in sg_ids) if sg_ids else '# No security groups found in scanner'

        hcl_diff = f"""
# Architectural Migration: Native AWS ASG Scheduling
resource "aws_launch_template" "__TF_ALIAS___tmpl" {{
    name_prefix   = "scheduled-tmpl-"
    image_id      = "{ami_id}"
    instance_type = "{instance_type if instance_type else 't3.micro'}"
    vpc_security_group_ids = {sg_ids}
}}

resource "aws_autoscaling_group" "__TF_ALIAS___asg" {{
  vpc_zone_identifier = ["{subnet_id}"]
  desired_capacity    = 1
  max_size            = 1
  min_size            = 0

  launch_template {{
    id      = aws_launch_template.__TF_ALIAS___tmpl.id
    version = "$Latest"
  }}
}}
resource "aws_autoscaling_schedule" "__TF_ALIAS___scale_down" {{
  scheduled_action_name  = "scale-down-evening"
  min_size               = 0
  max_size               = 0
  desired_capacity       = 0
  recurrence             = "0 19 * * MON-FRI"
  autoscaling_group_name = aws_autoscaling_group.__TF_ALIAS___asg.name
}}

resource "aws_autoscaling_schedule" "__TF_ALIAS___scale_up" {{
  scheduled_action_name  = "scale-up-morning"
  min_size               = 1
  max_size               = 1
  desired_capacity       = 1
  recurrence             = "0 8 * * MON-FRI"
  autoscaling_group_name = aws_autoscaling_group.__TF_ALIAS___asg.name
}}
        """
        return RuleResult(
            action=Action.TIER_3_IAC,
            confidence=Confidence.HIGH,
            reasoning="Workload maps perfectly to business hours. Wrapping in an ASG with cron-based scaling automates shutdowns safely.",
            blast_radius_assessment="LOW - Wrapping a single instance in an ASG requires minimal configuration.",
            estimated_monthly_savings=40.0,  # Saving ~65% by turning off nights and weekends
            hcl_edits=[HCLEdit(
                edit_type="replace_resource",
                resource_address="__TF_ADDRESS__",
                full_resource_hcl=hcl_diff.strip(),
            )],
            system_tasks=[],
        )

    if pattern == WorkloadPattern.ALWAYS_ON_IDLE:
        target_type = DOWNSIZE_MAP.get(instance_type)
        if target_type is None:
            return RuleResult(
                action=Action.IGNORE,
                confidence=Confidence.LOW,
                reasoning=f"No safe downsize path mapped for {instance_type}.",
                blast_radius_assessment="SAFE",
                estimated_monthly_savings=0.0,
            )

        target_cost = EC2_MONTHLY_PRICING.get(target_type, 0.0)
        savings = current_cost - target_cost if current_cost and target_cost else 0.0

        return RuleResult(
            action=Action.TIER_3_IAC,
            confidence=Confidence.HIGH,
            reasoning=f"Workload is consistently flat and over-provisioned. Downsizing to {target_type}.",
            blast_radius_assessment="MEDIUM - Requires instance reboot.",
            estimated_monthly_savings=savings,
            hcl_edits=[HCLEdit(
                edit_type="update_attribute",
                resource_address="__TF_ADDRESS__",
                attribute_path="instance_type",
                old_value=instance_type,
                new_value=target_type,
            )],
        )

    # Catch All
    return RuleResult(
        Action.IGNORE, Confidence.HIGH, "Resource is active and appropriately provisioned.", "SAFE", 0.0
    )
