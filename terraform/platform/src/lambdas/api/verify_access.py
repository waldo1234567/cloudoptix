import os
import json
import logging
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
sts = boto3.client('sts')
TABLE_NAME = os.environ['DYNAMODB_TABLE_NAME']
table = dynamodb.Table(TABLE_NAME)


def lambda_handler(event, context):
    """POST /api/v1/tenants/{id}/verify-access

    Confirms the tenant created the cross-account role correctly by attempting to
    assume it (with the profile's external id). Returns 200 with a `verified`
    flag either way so the UI can show a clear success/failure without treating a
    not-yet-created role as an API error.
    """
    try:
        tenant_id = (event.get('pathParameters') or {}).get('id')
        if not tenant_id:
            return _response(400, {"error": "Missing tenant id in path parameters."})

        profile = table.get_item(
            Key={'PK': f"TENANT#{tenant_id}", 'SK': "PROFILE"}
        ).get('Item')
        if not profile:
            return _response(404, {"error": "Tenant not found. Register first."})

        role_arn = profile.get('TenantRoleArn')
        external_id = profile.get('ExternalId', '')
        if not role_arn:
            return _response(200, {"verified": False, "message": "Tenant profile has no role ARN."})

        try:
            sts.assume_role(
                RoleArn=role_arn,
                RoleSessionName='cloudoptix-verify',
                ExternalId=external_id,
                DurationSeconds=900,
            )
            return _response(200, {
                "verified": True,
                "role_arn": role_arn,
                "message": "Successfully assumed the CloudOptix role.",
            })
        except Exception as e:
            logger.info(f"Assume-role verification failed for {tenant_id}: {e}")
            return _response(200, {
                "verified": False,
                "role_arn": role_arn,
                "message": (
                    "Could not assume the role yet. Make sure the CloudFormation stack "
                    "finished and the external ID matches."
                ),
            })

    except Exception as e:
        logger.error(f"Verify-access API Error: {e}", exc_info=True)
        return _response(500, {"error": "Internal Server Error."})


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }
