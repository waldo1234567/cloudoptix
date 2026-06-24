import os
import json
import time
import logging
from typing import List, Dict, Any
from datetime import datetime, timezone
from botocore.exceptions import ClientError
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
TABLE_NAME = os.environ.get('RECOMMENDATIONS_TABLE', 'cloudoptix-recommendations-prod')

def fetch_approved_rules_in_batches(tenant_id: str, rule_ids: List[str]) -> List[Dict[str,Any]]:
    fetched_items = []
    
    chunks = [rule_ids[i:i + 100] for i in range(0, len(rule_ids), 100)]
    
    for chunk in chunks:
        keys_to_get = [{'PK': f"TENANT#{tenant_id}", 'SK': f"RECOMMENDATION#{rule_id}"} for rule_id in chunk]
        
        request_items = {
            TABLE_NAME: {
                'Keys': keys_to_get,
                'ConsistentRead': False 
            }
        }
        
        retries = 0
        max_retries = 5
        
        while request_items and retries < max_retries:
            try:
                response = dynamodb.meta.client.batch_get_item(RequestItems=request_items) # type: ignore
                
                if TABLE_NAME in response.get('Responses', {}):
                    fetched_items.extend(response['Responses'][TABLE_NAME])
                
                unprocessed = response.get('UnprocessedKeys', {})
                if unprocessed and TABLE_NAME in unprocessed:
                    logger.warning(f"Throttled by DynamoDB. Retrying {len(unprocessed[TABLE_NAME]['Keys'])} keys.")
                    request_items = unprocessed
                    retries += 1
                    time.sleep((2 ** retries) * 0.1)
                    
                else:
                    request_items = None
                    
            except ClientError as e:
                logger.error(f"DynamoDB BatchGetItem failed: {e}")
                raise Exception("Failed to retrieve recommendations from database.")
            
        if request_items:
            logger.error("Max retries exceeded for DynamoDB BatchGetItem.")
            raise Exception("Database timeout. Some configurations could not be loaded.")
        
    return fetched_items

def compile_terraform(tenant_id: str, items: List[Dict[str, Any]]) -> str:
    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    
    compiled_hcl = f"""
# ==============================================================================
# CloudOptix Autonomous Architecture Plan
# Generated: {timestamp} UTC
# Tenant Context: {tenant_id}
# ==============================================================================
# INSTRUCTIONS:
# 1. Place this file in your target environment's Terraform directory.
# 2. Replace placeholder IDs (e.g., <your_resource_name>) with your actual 
#    Terraform resource addresses from your existing state.
# 3. Run `terraform plan` to review the architectural delta.
# ==============================================================================
"""
    grouped_changes = {
        "Compute (EC2 & ASG)": [],
        "Database (RDS & DynamoDB)": [],
        "Storage (EBS & EFS)": [],
        "Networking (NAT, ALB, EIP)": [],
        "Serverless & IAM": []
    }    
    
    for item in items:
        if item.get('Action') != 'TIER_3_IAC':
            continue
        
        res_type = item.get('ResourceType', 'unknown').lower()
        hcl_snippet = item.get('TerraformHclDiff', '').strip()
        reasoning = item.get('Reasoning', 'Architectural optimization.')
        res_id = item.get('ResourceId', 'Unknown')
        
        if not hcl_snippet:
            continue
        
        block = f"""
# ---------------------------------------------------------
# Target: {res_id}
# Strategy: {reasoning}
# ---------------------------------------------------------
{hcl_snippet}
"""
        if res_type in ['instance', 'autoscalinggroup']: 
            grouped_changes["Compute (EC2 & ASG)"].append(block)
        elif res_type in ['db-instance', 'table']: 
            grouped_changes["Database (RDS & DynamoDB)"].append(block)
        elif res_type in ['volume', 'filesystem']: 
            grouped_changes["Storage (EBS & EFS)"].append(block)
        elif res_type in ['natgateway', 'loadbalancer', 'eip']: 
            grouped_changes["Networking (NAT, ALB, EIP)"].append(block)
        elif res_type in ['function', 'role']: 
            grouped_changes["Serverless & IAM"].append(block)
        else: 
            grouped_changes["Compute (EC2 & ASG)"].append(block)
            
    
    for category, blocks in grouped_changes.items():
        if blocks:
            compiled_hcl += f"\n# {'='*70}\n# {category}\n# {'='*70}\n"
            compiled_hcl += "\n".join(blocks)
    
    return compiled_hcl


def lambda_handler(event, context):
    
    try:
        authorizer = event.get('requestContext', {}).get('authorizer', {})
        
        if 'jwt' in authorizer:
            claims = authorizer['jwt'].get('claims', {})
        else:
            claims = authorizer.get('claims', {})
        
        tenant_id =  claims.get('custom:tenant_id') or claims.get('sub') or claims.get('cognito:username')
        if not tenant_id:
            logger.critical("Unauthorized access attempt. No tenant identity found in request context.")
            return _build_response(401, {"error": "Unauthorized. Invalid JWT context."})
    except Exception as e:
        logger.error(f"Auth parsing error: {e}")
        return _build_response(500, {"error": "Authentication processing failure."})
    

    try:
        body = json.loads(event.get('body', '{}'))
        rule_ids = body.get('approved_rule_ids', [])
        
        if not isinstance(rule_ids, list) or len(rule_ids) == 0:
            return _build_response(400, {"error": "Malformed request. 'approved_rule_ids' must be a non-empty array."})
        
        if len(rule_ids) > 500:
            return _build_response(400, {"error": "Batch size limit exceeded. Please compile maximum 500 rules at a time."})
    
    except json.JSONDecodeError:
        return _build_response(400, {"error": "Invalid JSON payload."})
    
    
    try:
        logger.info(f"Compiling {len(rule_ids)} rules for Tenant {tenant_id}")
        raw_items= fetch_approved_rules_in_batches(tenant_id, rule_ids)
        
        if not raw_items:
            return _build_response(404, {"error": "No matching approved rules found in database."})
        compiled_terraform = compile_terraform(tenant_id, raw_items)
        
        return _build_response(200, {
            "message": "Terraform compiled successfully.",
            "file_content": compiled_terraform,
            "rule_count": len(raw_items)
        })
          
    except Exception as e:
        logger.error(f"Compiler API Error: {e}")
        return _build_response(500, {"error": "Internal Server Error during compilation."})                     

def _build_response(status_code: int, body_dict: Dict[str, Any]) -> Dict[str,Any]:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*", #TODO Replace with strict domain 
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization"
        },
        "body": json.dumps(body_dict)
    }
    