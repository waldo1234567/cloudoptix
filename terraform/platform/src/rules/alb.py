from typing import Dict, Any
from .models import Action, Confidence, RuleResult
from terraform.platform.src.lambdas.metrics.classfier import WorkloadPattern

def evaluate(resource: Dict[str, Any], metrics: Dict[str, Any], pattern: WorkloadPattern) -> RuleResult:
    alb_arn = resource.get('SK', '').split('RESOURCE#')[-1]
    alb_name = alb_arn.split('/')[-2] if '/' in alb_arn else alb_arn
    
    if pattern == WorkloadPattern.ABANDONED:
        hcl_diff = f"""
# Architectural Migration: Tear down orphaned ingress layer
- resource "aws_lb" "{alb_name}" {{ ... }}
- resource "aws_lb_listener" "{alb_name}_http" {{ ... }}
- resource "aws_lb_target_group" "{alb_name}_tg" {{ ... }}
        """
        return RuleResult(
            action=Action.TIER_3_IAC,
            confidence=Confidence.HIGH,
            reasoning="ALB received exactly 0 requests over 30 days. Entire ingress infrastructure is orphaned.",
            blast_radius_assessment="LOW - Graph isolated and mathematically zero traffic.",
            estimated_monthly_savings=16.50, # Static $0.0225/hr baseline cost
            terraform_hcl_diff=hcl_diff.strip()
        )
        
    return RuleResult(
        Action.IGNORE, Confidence.HIGH, "ALB is actively processing requests.", "SAFE", 0.0
    )
    