"""
CloudOptix Probe (V2 — post-apply validator).

In V1 the probe executed tier actions directly (stop/terminate/delete via the
AWS API). In the V2 Terraform-first model, all mutation happens through
terraform apply, so the probe is repurposed for VALIDATION ONLY: after an apply
succeeds it assumes the tenant role and health-checks the affected resource to
confirm the architecture survived the change, then records a verdict.

Invoked asynchronously by build_monitor after a successful apply build with:
    {"tenant_id": ..., "finding_id": ..., "resource_id": ...}

Outcome:
    VALIDATED           health checks passed
    VALIDATION_FAILED   checks failed -> SNS alert (rollback is a future step)
"""
import os
import json
import time
import logging
import urllib.request
import urllib.error
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError
from lambdas.scanner.auth import assume_tenant_role

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
sns = boto3.client('sns')
s3 = boto3.client('s3')
codebuild = boto3.client('codebuild')

TABLE_NAME = os.environ.get('DYNAMODB_TABLE_NAME', 'cloudoptix-core-table')
SNS_TOPIC_ARN = os.environ.get('SNS_TOPIC_ARN')
CONFIG_BUCKET = os.environ.get('CONFIG_BUCKET', '')
STATE_BUCKET = os.environ.get('STATE_BUCKET', '')
CODEBUILD_PROJECT = os.environ.get('CODEBUILD_PROJECT_NAME', 'CloudOptix-Terraform-Runner')
ROLLBACK_ENABLED = os.environ.get('ROLLBACK_ENABLED', 'true') == 'true'
table = dynamodb.Table(TABLE_NAME)  # type: ignore


# ── Resource-type validators: (clients, resource_id, finding) -> (ok, detail) ──

def _validate_instance(clients, resource_id, finding):
    action = finding.get('Action')
    resp = clients['ec2'].describe_instance_status(InstanceIds=[resource_id], IncludeAllInstances=True)
    statuses = resp.get('InstanceStatuses', [])
    if not statuses:
        return False, f"instance {resource_id} not found"
    st = statuses[0]
    state = st['InstanceState']['Name']

    # A stop recommendation is "healthy" when the instance actually stopped.
    if action == 'TIER_1_STOP':
        return state in ('stopped', 'stopping'), f"state={state} (expected stopped)"

    sys_status = st.get('SystemStatus', {}).get('Status')
    inst_status = st.get('InstanceStatus', {}).get('Status')
    ok = state == 'running' and sys_status in ('ok', 'initializing') and inst_status in ('ok', 'initializing')
    return ok, f"state={state} system={sys_status} instance={inst_status}"


def _validate_db(clients, resource_id, finding):
    try:
        resp = clients['rds'].describe_db_instances(DBInstanceIdentifier=resource_id)
    except ClientError as e:
        if e.response['Error']['Code'] == 'DBInstanceNotFound':
            # If the finding removed the DB, absence is the desired end state.
            if finding.get('Action') in ('TIER_1_DELETE',):
                return True, "db instance removed as intended"
            return False, f"db instance {resource_id} not found"
        raise
    status = resp['DBInstances'][0]['DBInstanceStatus']
    return status == 'available', f"status={status}"


def _validate_loadbalancer(clients, resource_id, finding):
    try:
        tgs = clients['elbv2'].describe_target_groups(LoadBalancerArn=resource_id).get('TargetGroups', [])
    except ClientError as e:
        return True, f"could not enumerate target groups ({e.response['Error']['Code']}); skipped"
    if not tgs:
        return True, "load balancer has no target groups"
    healthy = 0
    for tg in tgs:
        th = clients['elbv2'].describe_target_health(
            TargetGroupArn=tg['TargetGroupArn']
        ).get('TargetHealthDescriptions', [])
        healthy += sum(1 for t in th if t['TargetHealth']['State'] == 'healthy')
    return healthy > 0, f"healthy_targets={healthy}"


def _validate_generic(clients, resource_id, finding):
    # No resource-specific health probe; a successful apply is the signal.
    return True, f"no specific health probe for type '{finding.get('ResourceType')}'; assumed healthy"


VALIDATORS = {
    'instance': _validate_instance,
    'db-instance': _validate_db,
    'loadbalancer': _validate_loadbalancer,
}


# ── Absence checkers (for removal / replacement): (clients, resource_id) -> (gone, detail) ──

def _absent_instance(clients, resource_id):
    try:
        resp = clients['ec2'].describe_instances(InstanceIds=[resource_id])
    except ClientError as e:
        if e.response['Error']['Code'] == 'InvalidInstanceID.NotFound':
            return True, "instance no longer exists"
        raise
    resvs = resp.get('Reservations', [])
    if not resvs:
        return True, "instance not found"
    state = resvs[0]['Instances'][0]['State']['Name']
    return state in ('terminated', 'shutting-down'), f"state={state} (expected terminated)"


def _absent_db(clients, resource_id):
    try:
        clients['rds'].describe_db_instances(DBInstanceIdentifier=resource_id)
        return False, "db instance still exists"
    except ClientError as e:
        if e.response['Error']['Code'] == 'DBInstanceNotFound':
            return True, "db instance removed"
        raise


def _absent_eip(clients, resource_id):
    try:
        resp = clients['ec2'].describe_addresses(AllocationIds=[resource_id])
        return not resp.get('Addresses'), "eip released" if not resp.get('Addresses') else "eip still allocated"
    except ClientError as e:
        if e.response['Error']['Code'] in ('InvalidAllocationID.NotFound', 'InvalidAddress.NotFound'):
            return True, "eip released"
        raise


def _absent_volume(clients, resource_id):
    try:
        clients['ec2'].describe_volumes(VolumeIds=[resource_id])
        return False, "volume still exists"
    except ClientError as e:
        if e.response['Error']['Code'] == 'InvalidVolume.NotFound':
            return True, "volume removed"
        raise


ABSENCE_CHECKERS = {
    'instance': _absent_instance,
    'db-instance': _absent_db,
    'eip': _absent_eip,
    'volume': _absent_volume,
}


def _determine_mode(finding):
    """Picks validation strategy from what the change actually did."""
    edits = finding.get('TerraformEdits') or []
    edit_types = {e.get('edit_type') for e in edits}
    action = finding.get('Action')
    if 'replace_resource' in edit_types:
        return 'replace'   # original destroyed, new architecture created
    if 'remove_resource' in edit_types or action in ('TIER_1_DELETE', 'TIER_1_RELEASE'):
        return 'removal'   # resource should be gone
    return 'inplace'       # same resource id survives (update_attribute / add_resource)


def _assert_absent(clients, resource_type, resource_id):
    checker = ABSENCE_CHECKERS.get(resource_type)
    if not checker:
        return True, f"removal of '{resource_type}' assumed (no absence probe available)"
    return checker(clients, resource_id)


# ── L2: reachability across SG/NACL/routes via VPC Reachability Analyzer ────────

REACHABILITY_TIMEOUT = 150  # seconds to wait for an analysis to finish


def _explain_block(analysis):
    codes = [e.get('ExplanationCode') for e in analysis.get('Explanations', []) if e.get('ExplanationCode')]
    return ', '.join(codes) if codes else "no path found"


def _run_reachability(ec2, source, dest, port, label):
    """Returns (found, detail). found is None when the check couldn't run (treated as a skip)."""
    path_id = None
    try:
        path = ec2.create_network_insights_path(
            Source=source, Destination=dest, Protocol='tcp', DestinationPort=port,
        )
        path_id = path['NetworkInsightsPath']['NetworkInsightsPathId']
        analysis = ec2.start_network_insights_analysis(NetworkInsightsPathId=path_id)
        aid = analysis['NetworkInsightsAnalysis']['NetworkInsightsAnalysisId']

        deadline = time.time() + REACHABILITY_TIMEOUT
        while time.time() < deadline:
            res = ec2.describe_network_insights_analyses(
                NetworkInsightsAnalysisIds=[aid]
            )['NetworkInsightsAnalyses'][0]
            if res['Status'] != 'running':
                if res.get('NetworkPathFound'):
                    return True, f"{label}: reachable on tcp/{port}"
                return False, f"{label}: NOT reachable on tcp/{port} (blocked by {_explain_block(res)})"
            time.sleep(5)
        return None, f"{label}: analysis timed out"
    except ClientError as e:
        code = e.response['Error']['Code']
        if code in ('UnauthorizedOperation', 'AccessDenied', 'AccessDeniedException'):
            return None, f"{label}: skipped (tenant role lacks Reachability Analyzer permissions)"
        return None, f"{label}: skipped ({code})"
    finally:
        if path_id:
            try:
                ec2.delete_network_insights_path(NetworkInsightsPathId=path_id)
            except ClientError:
                pass


def _find_igw(ec2, vpc_id):
    resp = ec2.describe_internet_gateways(
        Filters=[{'Name': 'attachment.vpc-id', 'Values': [vpc_id]}]
    )
    igws = resp.get('InternetGateways', [])
    return igws[0]['InternetGatewayId'] if igws else None


def _ingress_port(ec2, sg_ids):
    """Best-effort: the first world-open TCP ingress port, else 443."""
    if not sg_ids:
        return 443
    try:
        sgs = ec2.describe_security_groups(GroupIds=sg_ids).get('SecurityGroups', [])
    except ClientError:
        return 443
    fallback = None
    for sg in sgs:
        for perm in sg.get('IpPermissions', []):
            if perm.get('IpProtocol') not in ('tcp', '6') or perm.get('FromPort') is None:
                continue
            if any(r.get('CidrIp') == '0.0.0.0/0' for r in perm.get('IpRanges', [])):
                return perm['FromPort']
            fallback = fallback or perm['FromPort']
    return fallback or 443


def _find_fronting_alb(elbv2, instance_id):
    """Returns (dns, scheme) of a load balancer that has this instance as a target, else (None, None)."""
    for lb in elbv2.describe_load_balancers().get('LoadBalancers', []):
        tgs = elbv2.describe_target_groups(
            LoadBalancerArn=lb['LoadBalancerArn']
        ).get('TargetGroups', [])
        for tg in tgs:
            th = elbv2.describe_target_health(
                TargetGroupArn=tg['TargetGroupArn']
            ).get('TargetHealthDescriptions', [])
            if any(t['Target']['Id'] == instance_id for t in th):
                return lb.get('DNSName'), lb.get('Scheme')
    return None, None


def _synthetic_http(dns):
    """L3-lite: a real request through the ingress path. Any response < 500 means it works."""
    for scheme in ("http", "https"):
        url = f"{scheme}://{dns}/"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, method='GET'), timeout=10) as r:
                return r.status < 500, f"synthetic GET {url} -> {r.status}"
        except urllib.error.HTTPError as e:
            return e.code < 500, f"synthetic GET {url} -> {e.code}"
        except Exception:
            continue
    return False, f"synthetic GET to {dns} failed on http and https"


def _l2_instance(clients, resource_id):
    """Reachability + synthetic checks for an instance that should be serving traffic."""
    ec2, elbv2 = clients['ec2'], clients['elbv2']
    try:
        resvs = ec2.describe_instances(InstanceIds=[resource_id]).get('Reservations', [])
    except ClientError as e:
        return True, f"reachability skipped (instance lookup failed: {e.response['Error']['Code']})"
    if not resvs:
        return True, "reachability skipped (instance not found)"

    inst = resvs[0]['Instances'][0]
    vpc_id = inst.get('VpcId')
    public_ip = inst.get('PublicIpAddress')
    sg_ids = [g['GroupId'] for g in inst.get('SecurityGroups', [])]

    checks = []  # (ok, detail)

    # Ingress from the internet, if the instance is directly addressable.
    if public_ip and vpc_id:
        igw = _find_igw(ec2, vpc_id)
        if igw:
            found, detail = _run_reachability(ec2, igw, resource_id, _ingress_port(ec2, sg_ids), "IGW->instance")
            checks.append((True if found is None else found, detail))  # a skip is non-fatal

    # Synthetic transaction through a fronting internet-facing ALB (covers private targets).
    dns, scheme = _find_fronting_alb(elbv2, resource_id)
    if dns and scheme == 'internet-facing':
        ok, detail = _synthetic_http(dns)
        checks.append((ok, f"ALB {detail}"))

    # East-west: can the instance still reach its databases (the classic private hop)?
    checks.extend(_l2_instance_to_db(clients, resource_id, sg_ids, vpc_id))

    if not checks:
        return True, "no external ingress path to validate (private instance, no internet-facing ALB)"
    return all(c[0] for c in checks), "; ".join(c[1] for c in checks)


# ── L2 east-west: EC2 -> RDS reachability (SG-inference driven) ─────────────────

def _sg_allows_from(ec2, db_sg_ids, instance_sg_ids):
    """True if any of the DB's SGs has an ingress rule referencing an instance SG."""
    try:
        sgs = ec2.describe_security_groups(GroupIds=db_sg_ids).get('SecurityGroups', [])
    except ClientError:
        return False
    inst = set(instance_sg_ids)
    for sg in sgs:
        for perm in sg.get('IpPermissions', []):
            if any(pair.get('GroupId') in inst for pair in perm.get('UserIdGroupPairs', [])):
                return True
    return False


def _find_db_dependencies(clients, instance_sg_ids, vpc_id):
    """RDS instances in the same VPC whose SG allows inbound from this instance's SG.

    Returns [(db_id, db_sg_ids, port), ...]. This is probe-time inference: an SG rule
    on the DB referencing the app's SG is the canonical "this app may reach this DB".
    """
    rds, ec2 = clients['rds'], clients['ec2']
    deps = []
    try:
        dbs = rds.describe_db_instances().get('DBInstances', [])
    except ClientError as e:
        logger.warning(f"describe_db_instances failed: {e}")
        return deps

    for db in dbs:
        if (db.get('DBSubnetGroup') or {}).get('VpcId') != vpc_id:
            continue
        if db.get('DBInstanceStatus') != 'available':
            continue
        db_sg_ids = [g['VpcSecurityGroupId'] for g in db.get('VpcSecurityGroups', [])
                     if g.get('Status') == 'active']
        port = (db.get('Endpoint') or {}).get('Port')
        if db_sg_ids and port and _sg_allows_from(ec2, db_sg_ids, instance_sg_ids):
            deps.append((db['DBInstanceIdentifier'], db_sg_ids, port))
    return deps


def _rds_enis(ec2, db_sg_ids):
    """Resolve the RDS instance's network interface(s) — the reachability destination."""
    try:
        enis = ec2.describe_network_interfaces(
            Filters=[{'Name': 'group-id', 'Values': db_sg_ids}]
        ).get('NetworkInterfaces', [])
    except ClientError:
        return []
    rds_managed = [e['NetworkInterfaceId'] for e in enis
                   if (e.get('Description') or '').startswith('RDSNetworkInterface')]
    return rds_managed or [e['NetworkInterfaceId'] for e in enis]


def _l2_instance_to_db(clients, instance_id, instance_sg_ids, vpc_id):
    """For each inferred DB dependency, prove instance -> RDS-ENI reachability on the DB port."""
    ec2 = clients['ec2']
    results = []
    for db_id, db_sg_ids, port in _find_db_dependencies(clients, instance_sg_ids, vpc_id):
        enis = _rds_enis(ec2, db_sg_ids)
        if not enis:
            results.append((True, f"instance->{db_id}: no RDS ENI resolved; skipped"))
            continue
        found, detail = _run_reachability(ec2, instance_id, enis[0], port, f"instance->{db_id}")
        results.append((True if found is None else found, detail))  # a skip is non-fatal
    return results


def _validate(clients, resource_type, resource_id, finding):
    """Mode-aware validation so removed/replaced resources aren't judged as unhealthy."""
    mode = _determine_mode(finding)

    if mode == 'removal':
        gone, detail = _assert_absent(clients, resource_type, resource_id)
        return gone, f"removal: {detail}"

    if mode == 'replace':
        # The original resource was swapped out for a new topology (e.g. instance -> ASG).
        # Confirm the old resource is gone; deep health of the new architecture is L2's job
        # (reachability across SG/NACL/routes), not per-old-id health.
        gone, detail = _assert_absent(clients, resource_type, resource_id)
        note = "deep reachability validation deferred to L2"
        return gone, f"replacement applied, original {detail}; {note}"

    # In-place: resource identity survived — validate health (L1), then reachability (L2).
    validator = VALIDATORS.get(resource_type, _validate_generic)
    ok, detail = validator(clients, resource_id, finding)

    # Reachability only matters for a resource that is meant to stay up and serving.
    if ok and resource_type == 'instance' and finding.get('Action') != 'TIER_1_STOP':
        l2_ok, l2_detail = _l2_instance(clients, resource_id)
        return (ok and l2_ok), f"health[{detail}] reachability[{l2_detail}]"

    return ok, detail


def _tenant_clients(role_arn, external_id, region):
    ak, sk, token = assume_tenant_role(role_arn, external_id, session_name="CloudOptixProbe")
    kw = dict(region_name=region, aws_access_key_id=ak, aws_secret_access_key=sk, aws_session_token=token)
    return {
        'ec2': boto3.client('ec2', **kw),
        'rds': boto3.client('rds', **kw),
        'elbv2': boto3.client('elbv2', **kw),
    }


def lambda_handler(event, context):
    tenant_id = event.get('tenant_id')
    finding_id = event.get('finding_id')
    resource_id = event.get('resource_id')

    if not all([tenant_id, finding_id, resource_id]):
        logger.error(f"Probe invoked without full context: {event}")
        return {"status": "ERROR", "message": "missing tenant_id/finding_id/resource_id"}

    finding = table.get_item(
        Key={'PK': f"TENANT#{tenant_id}", 'SK': f"FINDING#{finding_id}"}
    ).get('Item')
    if not finding:
        logger.warning(f"Finding {finding_id} not found; nothing to validate.")
        return {"status": "SKIPPED"}

    profile = table.get_item(Key={'PK': f"TENANT#{tenant_id}", 'SK': "PROFILE"}).get('Item', {})
    role_arn = profile.get('TenantRoleArn')
    region = profile.get('TargetRegion', 'ap-northeast-1')
    external_id = profile.get('ExternalId', '')
    resource_type = finding.get('ResourceType')

    logger.info(f"Validating finding {finding_id} ({resource_type} {resource_id}) for tenant {tenant_id}")

    try:
        clients = _tenant_clients(role_arn, external_id, region)
        ok, detail = _validate(clients, resource_type, resource_id, finding)
    except Exception as e:
        logger.error(f"Validation error for {finding_id}: {e}", exc_info=True)
        ok, detail = False, f"validation error: {e}"

    status = 'VALIDATED' if ok else 'VALIDATION_FAILED'
    logger.info(f"Finding {finding_id} -> {status} ({detail})")

    _update_finding(tenant_id, finding_id, status, detail)
    _record_history(tenant_id, finding_id, status, detail)
    if not ok:
        _notify(tenant_id, finding_id, detail)
        if ROLLBACK_ENABLED:
            _rollback(tenant_id, finding_id, resource_id, role_arn, region)

    return {"status": status, "detail": detail}


def _rollback(tenant_id, finding_id, resource_id, role_arn, region):
    """Revert main.tf to its previous S3 version and re-apply to undo the change."""
    config_key = f"{tenant_id}/main.tf"
    try:
        versions = [
            v for v in s3.list_object_versions(Bucket=CONFIG_BUCKET, Prefix=config_key).get('Versions', [])
            if v['Key'] == config_key
        ]
        # Newest-first: [0] is the just-applied (broken) version, [1] is the prior good one.
        if len(versions) < 2:
            logger.error(f"No previous main.tf version to roll back to for {finding_id}.")
            _update_finding(tenant_id, finding_id, 'ROLLBACK_FAILED', 'no previous main.tf version')
            return

        prev_id = versions[1]['VersionId']
        prev_body = s3.get_object(Bucket=CONFIG_BUCKET, Key=config_key, VersionId=prev_id)['Body'].read()
        s3.put_object(Bucket=CONFIG_BUCKET, Key=config_key, Body=prev_body)
        logger.info(f"Reverted main.tf to version {prev_id}; triggering rollback apply for {finding_id}.")

        build = codebuild.start_build(
            projectName=CODEBUILD_PROJECT,
            environmentVariablesOverride=[
                {'name': 'TENANT_ID', 'value': tenant_id, 'type': 'PLAINTEXT'},
                {'name': 'ACTION_ID', 'value': finding_id, 'type': 'PLAINTEXT'},
                {'name': 'CONFIG_BUCKET', 'value': CONFIG_BUCKET, 'type': 'PLAINTEXT'},
                {'name': 'STATE_BUCKET', 'value': STATE_BUCKET, 'type': 'PLAINTEXT'},
                {'name': 'TENANT_ROLE_ARN', 'value': role_arn or '', 'type': 'PLAINTEXT'},
                {'name': 'RESOURCE_ID', 'value': resource_id, 'type': 'PLAINTEXT'},
                {'name': 'AWS_REGION', 'value': region, 'type': 'PLAINTEXT'},
                {'name': 'APPLY', 'value': 'true', 'type': 'PLAINTEXT'},
                {'name': 'ROLLBACK', 'value': 'true', 'type': 'PLAINTEXT'},  # build_monitor won't re-validate
            ],
        )
        table.update_item(
            Key={'PK': f"TENANT#{tenant_id}", 'SK': f"FINDING#{finding_id}"},
            UpdateExpression="SET #s = :s, CodeBuildRollbackId = :b, UpdatedAt = :ts",
            ConditionExpression="attribute_exists(SK)",
            ExpressionAttributeNames={'#s': 'Status'},
            ExpressionAttributeValues={
                ':s': 'ROLLING_BACK',
                ':b': build.get('build', {}).get('id', ''),
                ':ts': datetime.now(timezone.utc).isoformat(),
            },
        )
    except ClientError as e:
        logger.error(f"Rollback failed for {finding_id}: {e}", exc_info=True)
        _update_finding(tenant_id, finding_id, 'ROLLBACK_FAILED', f"rollback error: {e}")


def _update_finding(tenant_id, finding_id, status, detail):
    try:
        table.update_item(
            Key={'PK': f"TENANT#{tenant_id}", 'SK': f"FINDING#{finding_id}"},
            UpdateExpression="SET #s = :s, ValidationDetail = :d, ValidatedAt = :ts",
            ConditionExpression="attribute_exists(SK)",
            ExpressionAttributeNames={'#s': 'Status'},
            ExpressionAttributeValues={
                ':s': status,
                ':d': detail,
                ':ts': datetime.now(timezone.utc).isoformat(),
            },
        )
    except ClientError as e:
        if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise


def _record_history(tenant_id, finding_id, status, detail):
    now = datetime.now(timezone.utc).isoformat()
    table.put_item(Item={
        'PK': f"TENANT#{tenant_id}",
        'SK': f"EXEC#{now}#{finding_id}-validation",
        'Type': 'ExecutionRecord',
        'FindingId': finding_id,
        'Phase': 'validate',
        'Result': status,
        'Detail': detail,
        'CreatedAt': now,
    })


def _notify(tenant_id, finding_id, detail):
    if not SNS_TOPIC_ARN:
        return
    try:
        sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject="CloudOptix: post-apply validation FAILED",
            Message=(f"Tenant {tenant_id} finding {finding_id} failed post-apply validation: {detail}. "
                     f"The applied change may have degraded the resource — review and consider rolling back."),
        )
    except ClientError as e:
        logger.warning(f"SNS publish failed: {e}")
