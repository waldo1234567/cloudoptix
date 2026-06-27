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
CORE_TABLE_NAME = os.environ.get('DYNAMODB_TABLE_NAME', 'cloudoptix-core-table')

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
                    break
                    
            except ClientError as e:
                logger.error(f"DynamoDB BatchGetItem failed: {e}")
                raise Exception("Failed to retrieve recommendations from database.")
            
    return fetched_items

def compile_terraform(tenant_id: str, raw_items: List[Dict[str, Any]]) -> str:
    resource_ids = [item.get('ResourceId') for item in raw_items if item.get('ResourceId')]
    state_map = {}
    if resource_ids:
        chunks = [resource_ids[i:i + 100] for i in range(0, len(resource_ids), 100)]
        for chunk in chunks:
            keys_to_get = [{'PK': f"TENANT#{tenant_id}", 'SK': f"STATEADDR#{res_id}"} for res_id in chunk]
            request_items = { CORE_TABLE_NAME: {'Keys': keys_to_get, 'ConsistentRead': False} }
            
            try:
                response = dynamodb.meta.client.batch_get_item(RequestItems=request_items) # type: ignore
                for item in response.get('Responses', {}).get(CORE_TABLE_NAME, []):
                    res_id = item['SK'].split('STATEADDR#')[-1]
                    state_map[res_id] = item.get('TerraformAddress')
            except ClientError as e:
                logger.error(f"Failed to fetch State Addresses: {e}")
    
    assembled_hcl = []

    for item in raw_items:
        res_id = item.get('ResourceId', 'unknown')
        res_type = item.get('ResourceType', 'resource')
        raw_hcl = item.get('TerraformHCL', '')
        
        tf_address = state_map.get(res_id)
        
        if tf_address:
            final_block = raw_hcl.replace('__TF_ALIAS__', tf_address)
            assembled_hcl.append(final_block)
        else:
            safe_alias = f"aws_{res_type.replace('-', '_')}.cloudoptix_imported"
            fallback_msg = f"""
# ==============================================================================
# CLOUDOPTIX FALLBACK: UNMANAGED RESOURCE
# Resource: {res_id}
# Warning: This resource is missing from your terraform.tfstate. 
# It was likely created via the AWS Console manually.
# To safely apply this architectural migration, run this command first:
# 
# terraform import {tf_address or safe_alias} {res_id}
# ==============================================================================
"""
            final_block = fallback_msg + raw_hcl.replace('__TF_ALIAS__', safe_alias)
            assembled_hcl.append(final_block)
            
    return "\n\n".join(assembled_hcl)

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
        rule_ids = body.get('rule_ids', [])
        
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
    