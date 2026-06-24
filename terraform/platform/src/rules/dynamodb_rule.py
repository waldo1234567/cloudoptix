from typing import Dict, Any
from .models import Action, Confidence, RuleResult
from terraform.platform.src.lambdas.metrics.classfier import WorkloadPattern

def evaluate(resource: Dict[str, Any], metrics: Dict[str, Any], pattern: WorkloadPattern) -> RuleResult:
    meta = resource.get('RawMetadata', {})
    table_name = resource.get('SK', '').split('RESOURCE#')[-1]
    
    billing_mode = meta.get('BillingModeSummary', {}).get('BillingMode', 'PROVISIONED')
    
    # TODO [PROD-BLOCKER]: Replace static $0.00013 estimate with Async SSM Parameter Store cache fetch.
    
    estimated_monthly_savings = 15.0
    
    if pattern == WorkloadPattern.ABANDONED:
        if billing_mode == 'PROVISIONED':
            hcl_diff = f"""
            # Architectural Migration: Stop hourly bleeding on an abandoned table
resource "aws_dynamodb_table" "{table_name}" {{
-  billing_mode = "PROVISIONED"
+  billing_mode = "PAY_PER_REQUEST"
   # Note: Read/Write capacity blocks must be removed when using PAY_PER_REQUEST
}}           
            """
            
            return RuleResult(
                action=Action.TIER_3_IAC,
                confidence=Confidence.HIGH,
                reasoning="Table has zero traffic but is incurring flat hourly charges. Flipping to On-Demand drops the cost to exactly $0/month while preserving data.",
                blast_radius_assessment="LOW - Zero downtime modification. Data is perfectly preserved.",
                estimated_monthly_savings=estimated_monthly_savings,
                terraform_hcl_diff=hcl_diff.strip()
            )          
    
    if pattern == WorkloadPattern.SPIKY:
        if billing_mode == 'PROVISIONED':
            hcl_diff = f"""
            # Architectural Migration: Match billing to extreme traffic variance
resource "aws_dynamodb_table" "{table_name}" {{
-  billing_mode = "PROVISIONED"
+  billing_mode = "PAY_PER_REQUEST"
}}
            """
        
            return RuleResult(
                action=Action.TIER_3_IAC,
                confidence=Confidence.HIGH,
                reasoning="Workload exhibits extreme variance (high peaks, long troughs). On-Demand billing eliminates the waste of provisioned capacity during idle periods.",
                blast_radius_assessment="LOW - Zero downtime modification. AWS handles capacity automatically.",
                estimated_monthly_savings=estimated_monthly_savings,
                terraform_hcl_diff=hcl_diff.strip()
            )    
    
    if pattern == WorkloadPattern.ALWAYS_ON_ACTIVE:
        if billing_mode == 'PAY_PER_REQUEST':
            
            read_avg = metrics.get('ConsumedReadCapacityUnits', {}).get('Average', 5)
            write_avg = metrics.get('ConsumedWriteCapacityUnits', {}).get('Average', 5)
            
            safe_read = int(read_avg * 1.2)
            safe_write = int(write_avg * 1.2)
            
            hcl_diff = f"""
resource "aws_dynamodb_table" "{table_name}" {{
-  billing_mode = "PAY_PER_REQUEST"
+  billing_mode = "PROVISIONED"
+  read_capacity  = {safe_read}
+  write_capacity = {safe_write}
}}
            """
            return RuleResult(
                action=Action.TIER_3_IAC,
                confidence=Confidence.MEDIUM,
                reasoning=f"Table has sustained, predictable heavy traffic. Provisioned capacity with Auto Scaling will be significantly cheaper than On-Demand.",
                blast_radius_assessment="MEDIUM - Requires configuring DynamoDB Auto Scaling to accompany the Provisioned limits.",
                estimated_monthly_savings=estimated_monthly_savings, 
                terraform_hcl_diff=hcl_diff.strip()
            )
            
    return RuleResult(
        Action.IGNORE, Confidence.HIGH, f"Table is optimally configured for its workload pattern ({billing_mode}).", "SAFE", 0.0
    )
            