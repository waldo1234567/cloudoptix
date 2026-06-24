import os
import sys

# Ensure the src directory is in the path
sys.path.append(os.path.join(os.path.dirname(__file__), 'src'))

from terraform.platform.src.lambdas.scanner.main import handler as scanner_handler
from terraform.platform.src.lambdas.graph.builder import handler as graph_handle

TENANT_ID = "test-tenant-001"
ACCOUNT_ID = "247448832186"
REGION = "ap-northeast-1"
ROLE_ARN = "arn:aws:iam::247448832186:role/CloudOptixCrossAccountRole"
EXTERNAL_ID = "test-tenant-123"

def run_test():
    print(f"=== Starting Local Test Pipeline for Tenant: {TENANT_ID} ===")
    
    scanner_event = {
        "tenant_id": TENANT_ID,
        "account_id": ACCOUNT_ID,
        "region": REGION,
        "role_arn": ROLE_ARN,
        "external_id": EXTERNAL_ID
    }
    
    print("\n[PHASE 1] Executing Resource Inventory Scanner...")
    try:
        scanner_result = scanner_handler(scanner_event, context=None)
        print(f"Scanner Result: {scanner_result}")
    except Exception as e:
        print(f"Scanner Failed: {e}")
        return
    
    graph_event = {
        "tenant_id": TENANT_ID,
        "region": REGION,
        "role_arn": ROLE_ARN,
        "external_id": EXTERNAL_ID
    }
    
    print("\n[PHASE 1.5] Executing Dependency Graph Builder...")
    try:
        graph_result = graph_handle(graph_event, context=None)
        print(f"Graph Result: {graph_result}")
    except Exception as e:
        print(f"Graph Builder Failed: {e}")

if __name__ == "__main__":
    run_test()