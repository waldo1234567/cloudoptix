from typing import Dict, Any
from .models import Action, Confidence, RuleResult
from terraform.platform.src.lambdas.metrics.classfier import WorkloadPattern

def evaluate(resource: Dict[str, Any], metrics: Dict[str, Any], pattern: WorkloadPattern) -> RuleResult:
    nat_id = resource.get('SK', '').split('RESOURCE#')[-1]
    meta = resource.get('RawMetadata', {})
    state = meta.get('State', '')
    
    associated_route_tables = meta.get('AssociatedRouteTables', [])
    vpc_endpoints_present = meta.get('VpcEndpointsPresent', False)
    
    if state != 'available':
        return RuleResult(Action.IGNORE, Confidence.HIGH, "NAT Gateway is not in available state.", "SAFE", 0.0)
    
    if pattern == WorkloadPattern.ABANDONED:
        route_cleanup_hcl = ""
        for rt_id in associated_route_tables:
            route_cleanup_hcl += f"- resource \"aws_route\" \"{rt_id}_nat_route\" {{ ... }}\n"

        endpoint_context = (
            "VPC Endpoints are already provisioned in this VPC. No loss of AWS API access." 
            if vpc_endpoints_present 
            else "If instances require AWS API access (e.g., S3), provision VPC Endpoints before applying."
        )

        hcl_diff = f"""
# Architectural Migration: Eradicate Abandoned NAT Gateway
# Context: {endpoint_context}

{route_cleanup_hcl}
- resource "aws_nat_gateway" "{nat_id}" {{ ... }}
- resource "aws_eip" "{nat_id}_eip" {{ ... }}
        """
        
        return RuleResult(
            action=Action.TIER_3_IAC,
            confidence=Confidence.HIGH,
            reasoning=f"NAT Gateway is orphaned (0 bytes). Generating complete cleanup IaC including {len(associated_route_tables)} Route Tables.",
            blast_radius_assessment="MEDIUM - Modifies subnet routing. Safe due to 0 byte metric confirmation.",
            estimated_monthly_savings=32.50,
            terraform_hcl_diff=hcl_diff.strip()
        )

    return RuleResult(Action.IGNORE, Confidence.HIGH, "NAT Gateway is actively routing traffic.", "SAFE", 0.0)
    
    
        