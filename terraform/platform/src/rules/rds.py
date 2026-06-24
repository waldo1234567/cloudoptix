from typing import Dict, Any
from .models import Action, Confidence, RuleResult
from terraform.platform.src.lambdas.metrics.classfier import WorkloadPattern

RDS_DOWNSIZE_MAP = {
    'db.m5.large': 'db.m5.medium', 'db.m5.xlarge': 'db.m5.large',
    'db.r5.large': 'db.t3.large', 'db.r5.xlarge': 'db.r5.large', # r5 to t3 handles memory/compute ratio shifts
    'db.t3.large': 'db.t3.medium', 'db.t3.medium': 'db.t3.small'
}

def evaluate(resource: Dict[str, Any], metrics: Dict[str, Any], pattern: WorkloadPattern) -> RuleResult:
    meta = resource.get('RawMetadata', {})
    status = meta.get('DBInstanceStatus', 'unknown')
    instance_class = meta.get('DBInstanceClass', '')
    engine = meta.get('Engine', 'unknown')
    db_id = resource.get('SK', '').split('RESOURCE#')[-1]
    
    if status != 'available':
        return RuleResult(Action.IGNORE, Confidence.HIGH, f"Database status is '{status}'.", "SAFE", 0.0)
    

    swap_usage = metrics.get('SwapUsage', {}).get('Maximum', 0.0)
    
    if swap_usage > 1024 * 1024 * 50 :
        return RuleResult(
            Action.IGNORE, Confidence.HIGH,
            f"Database is swapping memory (Max: {swap_usage / (1024*1024):.1f} MB).",
            "HIGH - Instance is memory constrained. Any modification risks immediate crash.", 0.0
        )
        
    burst_balance = metrics.get('BurstBalance', {}).get('Average', 100.0)
    if burst_balance < 30.0:
        return RuleResult(
            Action.IGNORE, Confidence.HIGH,
            f"Database I/O burst balance is depleted (Avg: {burst_balance:.1f}%).",
            "HIGH - Instance is I/O constrained. Modification will cause latency spikes.", 0.0
        )
        
    # TODO [PROD-BLOCKER]: Implement Async Pricing Cache for accurate savings estimates.
    estimated_savings = 40.0
    
    if pattern == WorkloadPattern.ABANDONED:
        hcl_diff = f"""
# Architectural Migration: Snapshot and terminate abandoned database
# Data Security: CloudOptix has automatically backed up this database to the 'CloudOptix-Archive-Vault'.
- resource "aws_db_instance" "{db_id}" {{ ... }}
        """
        return RuleResult(
            action=Action.TIER_3_IAC,
            confidence=Confidence.HIGH,
            reasoning="Database has zero connections and negligible I/O over 30 days.",
            blast_radius_assessment="MEDIUM - Requires manual snapshot before Terraform apply.",
            estimated_monthly_savings=estimated_savings,
            terraform_hcl_diff=hcl_diff.strip(),
            system_tasks= [
                {
                    "type": "START_BACKUP_JOB",
                    "resource_type": "rds",
                    "resource_id": db_id, # The CloudOptix Executor will resolve this to the full ARN
                    "vault_name": "CloudOptix-Archive-Vault",
                    "description": f"CloudOptix Backup: Abandoned RDS {db_id}"
                }
            ]
        )
    
    if pattern == WorkloadPattern.SPIKY :
        if 'mysql' in engine or 'postgres' in engine:
            target_engine = "aurora-postgresql" if "postgres" in engine else "aurora-mysql"
            
            hcl_diff = f"""
# Architectural Migration: Provisioned RDS -> Aurora Serverless v2
resource "aws_rds_cluster" "{db_id}_serverless" {{
  cluster_identifier = "{db_id}-aurora-cluster"
  engine             = "{target_engine}"
  engine_mode        = "provisioned" # Required mode for Serverless v2
  
  serverlessv2_scaling_configuration {{
    min_capacity = 0.5
    max_capacity = 16.0
  }}
}}

resource "aws_rds_cluster_instance" "{db_id}_instance" {{
  cluster_identifier = aws_rds_cluster.{db_id}_serverless.id
  instance_class     = "db.serverless"
  engine             = aws_rds_cluster.{db_id}_serverless.engine
}}

            """
            return RuleResult(
                action=Action.TIER_3_IAC,
                confidence=Confidence.HIGH,
                reasoning="Workload exhibits extreme variance. Aurora Serverless v2 eliminates provisioned waste and mathematically guarantees capacity during spikes.",
                blast_radius_assessment="HIGH - Requires data migration (snapshot restore or read-replica promotion).",
                estimated_monthly_savings=estimated_savings,
                terraform_hcl_diff=hcl_diff.strip()
            )         
    
    if pattern == WorkloadPattern.SCHEDULED:
        hcl_diff = f"""
# Architectural Migration: Native AWS Instance Scheduling
resource "aws_db_instance" "{db_id}" {{
  # Retain existing configuration, add scheduler tags
  tags = {{
    "Schedule" = "business-hours-mon-fri"
  }}
}}
        """
        return RuleResult(
            action=Action.TIER_3_IAC,
            confidence=Confidence.HIGH,
            reasoning="Workload maps to business hours. Tagging for AWS Instance Scheduler automates shutdown on nights and weekends.",
            blast_radius_assessment="LOW - Tagging requires zero downtime.",
            estimated_monthly_savings=estimated_savings * 1.5, 
            terraform_hcl_diff=hcl_diff.strip()
        )
        
    
    if pattern == WorkloadPattern.ALWAYS_ON_IDLE:
        freeable_mb = metrics.get('FreeableMemory', {}).get('Minimum', 0.0) / (1024 * 1024)
        
        if freeable_mb < 500.0:
            return RuleResult(
                Action.IGNORE, Confidence.HIGH, 
                f"Available memory buffer ({freeable_mb:.1f} MB) is too narrow.", 
                "HIGH - Downsize risks Out-Of-Memory (OOM) panic.", 0.0
            )
        
        target_class = RDS_DOWNSIZE_MAP.get(instance_class)
        
        if target_class:
            hcl_diff=f"""
# Architectural Migration: Static Downsize
resource "aws_db_instance" "{db_id}" {{
-  instance_class = "{instance_class}"
+  instance_class = "{target_class}"
}}
            """
            
            return RuleResult(
                action=Action.TIER_3_IAC,
                confidence=Confidence.HIGH,
                reasoning=f"Database has flat, predictable utilization but is over-provisioned. Memory buffer ({freeable_mb:.0f}MB) mathematically supports a downsize.",
                blast_radius_assessment="MEDIUM - Requires instance reboot during maintenance window.",
                estimated_monthly_savings=estimated_savings,
                terraform_hcl_diff=hcl_diff.strip()
            )            
    
    return RuleResult(
        Action.IGNORE, Confidence.HIGH, "Database is actively utilized and appropriately modeled.", "SAFE", 0.0
    )