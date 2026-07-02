import os
import json
import logging
import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client('s3')
CONFIG_BUCKET = os.environ['CONFIG_BUCKET']

# Only terraform sources are browsable/diffable through this endpoint.
ALLOWED_SUFFIXES = ('.tf', '.tfvars')


def lambda_handler(event, context):
    """Read-only workspace browser backing the diff view.

    Serves the tenant's terraform files straight out of the versioned config
    bucket so the frontend can diff any two object versions client-side:

      GET /api/v1/tenants/{id}/workspace?action=list_files
      GET /api/v1/tenants/{id}/workspace/{file}?action=list_versions
      GET /api/v1/tenants/{id}/workspace/{file}?action=get_content&version_id=...

    Everything is scoped under the "{tenant_id}/" prefix; the file name is
    validated so a caller cannot escape the prefix or read non-terraform keys.
    """
    try:
        path_params = event.get('pathParameters') or {}
        tenant_id = path_params.get('id')
        raw_file = path_params.get('file')

        query_params = event.get('queryStringParameters') or {}
        action = (query_params.get('action') or '').strip()

        if not tenant_id:
            return _response(400, {"error": "Missing tenant id in path parameters."})

        if action == 'list_files':
            return _list_files(tenant_id)

        # Remaining actions operate on a single file.
        file_name = _safe_file_name(raw_file)
        if file_name is None:
            return _response(400, {"error": "Invalid or missing file name."})

        if action == 'list_versions':
            return _list_versions(tenant_id, file_name)
        if action == 'get_content':
            version_id = query_params.get('version_id')
            return _get_content(tenant_id, file_name, version_id)

        return _response(400, {"error": f"Unsupported action: {action or '(none)'}."})

    except ClientError as e:
        logger.error(f"Workspace API S3 error: {e}", exc_info=True)
        return _response(502, {"error": "Failed to read workspace storage."})
    except Exception as e:
        logger.error(f"Workspace API Error: {e}", exc_info=True)
        return _response(500, {"error": "Internal Server Error."})


def _safe_file_name(raw_file):
    """Return a bare terraform file name, or None if it is unsafe/unsupported.

    API Gateway hands us the {file} path segment already URL-decoded. We reject
    anything with path separators or traversal so the caller stays pinned to the
    tenant prefix, and only allow terraform sources.
    """
    if not raw_file:
        return None
    name = raw_file.strip()
    if not name or name in ('.', '..'):
        return None
    if '/' in name or '\\' in name or '..' in name:
        return None
    if not name.endswith(ALLOWED_SUFFIXES):
        return None
    return name


def _list_files(tenant_id):
    prefix = f"{tenant_id}/"
    files = []
    paginator = s3.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=CONFIG_BUCKET, Prefix=prefix):
        for obj in page.get('Contents', []):
            key = obj['Key']
            rel = key[len(prefix):]
            # Skip nested paths and non-terraform artifacts.
            if not rel or '/' in rel:
                continue
            if not rel.endswith(ALLOWED_SUFFIXES):
                continue
            files.append(rel)

    files.sort()
    return _response(200, {"tenant_id": tenant_id, "files": files})


def _list_versions(tenant_id, file_name):
    key = f"{tenant_id}/{file_name}"
    versions = []
    paginator = s3.get_paginator('list_object_versions')
    for page in paginator.paginate(Bucket=CONFIG_BUCKET, Prefix=key):
        for v in page.get('Versions', []):
            # Prefix match can catch sibling keys sharing this stem; require exact.
            if v['Key'] != key:
                continue
            last_modified = v.get('LastModified')
            versions.append({
                "version_id": v['VersionId'],
                "last_modified": last_modified.isoformat() if last_modified else None,
                "is_latest": bool(v.get('IsLatest')),
                "size": v.get('Size', 0),
            })

    # Newest first so the frontend defaults to comparing the two most recent.
    versions.sort(key=lambda x: x['last_modified'] or '', reverse=True)
    return _response(200, {"tenant_id": tenant_id, "file": file_name, "versions": versions})


def _get_content(tenant_id, file_name, version_id):
    key = f"{tenant_id}/{file_name}"
    kwargs = {'Bucket': CONFIG_BUCKET, 'Key': key}
    if version_id:
        kwargs['VersionId'] = version_id

    try:
        obj = s3.get_object(**kwargs)
    except ClientError as e:
        code = e.response.get('Error', {}).get('Code', '')
        if code in ('NoSuchKey', 'NoSuchVersion', 'AccessDenied'):
            return _response(404, {"error": "Requested file version not found."})
        raise

    content = obj['Body'].read().decode('utf-8', errors='replace')
    return _response(200, {
        "tenant_id": tenant_id,
        "file": file_name,
        "version_id": version_id or obj.get('VersionId'),
        "content": content,
    })


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body),
    }
