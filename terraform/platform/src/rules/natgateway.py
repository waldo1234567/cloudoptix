from typing import Dict, Any
from .models import Action, Confidence, RuleResult
from lambdas.metrics.classfier import WorkloadPattern

def evaluate(resource: Dict[str, Any], metrics: Dict[str, Any], pattern: WorkloadPattern) -> RuleResult:
    nat_id = resource.get('SK', '').split('RESOURCE#')[-1]
    meta = resource.get('RawMetadata', {})
    state = meta.get('State', '')
    
    associated_route_tables = meta.get('AssociatedRouteTables', [])
    vpc_endpoints_present = meta.get('VpcEndpointsPresent', False)
    
    if state != 'available':
        return RuleResult(Action.IGNORE, Confidence.HIGH, "NAT Gateway is not in available state.", "SAFE", 0.0)
    
    if pattern == WorkloadPattern.ABANDONED:
        if associated_route_tables:
            endpoint_context = (
                "VPC Endpoints are already provisioned in this VPC. No loss of AWS API access."
                if vpc_endpoints_present
                else "Provision VPC Endpoints before applying if instances require AWS API access (S3, DynamoDB)."
            )
            
            hcl_diff = f"""
# Architectural Migration: Remove Abandoned NAT Gateway
# Context: {endpoint_context}
# Route tables referencing this NAT must be updated first:
{"".join(f'# - {rt_id}{chr(10)}' for rt_id in associated_route_tables)}
- resource "aws_nat_gateway" "{nat_id}" {{ ... }}
- resource "aws_eip" "{nat_id}_eip" {{ ... }}
            """

            return RuleResult(
                action=Action.TIER_3_IAC,
                confidence=Confidence.HIGH,
                reasoning=f"NAT Gateway has zero traffic but {len(associated_route_tables)} route table(s) still reference it. Manual route cleanup required before deletion.",
                blast_radius_assessment="MEDIUM - Active route table references must be removed first.",
                estimated_monthly_savings=32.50,
                terraform_hcl_diff=hcl_diff.strip()
            )
        
        return RuleResult(
            action=Action.TIER_1_DELETE,
            confidence=Confidence.HIGH,
            reasoning="NAT Gateway has zero bytes and zero connections over 30 days with no route table dependencies. Safe for autonomous deletion.",
            blast_radius_assessment="LOW - No route dependencies confirmed. $32.50/month waste eliminated.",
            estimated_monthly_savings=32.50
        )

    return RuleResult(Action.IGNORE, Confidence.HIGH, "NAT Gateway is actively routing traffic.", "SAFE", 0.0)
    
    
        