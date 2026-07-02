#!/usr/bin/env python3
"""
CloudOptix end-to-end pipeline integration test.

Drives the REAL deployed back-half of the V2 pipeline against a REAL AWS
resource in the tenant account, seeding synthetic DynamoDB/S3 state to skip the
scanner/metrics/rules stages:

    seed PROFILE + workspace  ->  CodeBuild bootstrap apply (creates the real
    resource + tfstate)  ->  state_parser writes STATEADDR#  ->  seed FINDING#
    (update_attribute edit)  ->  SQS action_queue  ->  hcl_writer edits main.tf
    + plan  ->  PENDING_APPROVAL  ->  approve  ->  apply  ->  build_monitor  ->
    probe (post-apply validation)  ->  assert the resource changed + VALIDATED.

Scenarios (--scenario):
  param     (default)  a single SSM parameter; value flips before -> after.
                       Fast/cheap; exercises the whole control plane but not L2.
  instance             a real VPC + IGW + subnet + SG + EC2 instance; the finding
                       downsizes instance_type. Exercises the probe's L2
                       reachability (VPC Reachability Analyzer IGW->instance) and
                       tears everything down with a destroy apply. Costs a little
                       and takes longer (a few CodeBuild runs).

Run from PLATFORM-account creds (the control plane lives there). Verification and
cleanup talk to the TENANT account (the script assumes --tenant-role-arn, or use
--tenant-profile).
"""
import argparse
import json
import re
import sys
import time
import uuid
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

CORE_TABLE_DEFAULT        = "cloudoptix-core-table"
CODEBUILD_PROJECT_DEFAULT = "CloudOptix-Terraform-Runner"
ACTION_QUEUE_NAME_DEFAULT = "cloudoptix-action-queue.fifo"
APPROVE_LAMBDA_DEFAULT    = "CloudOptix-API-Approve"
TENANT_MGMT_LAMBDA_DEFAULT = "CloudOptix-Tenant-Mgmt"
TF_UPLOAD_LAMBDA_DEFAULT   = "CloudOptix-TF-Upload"
CONFIG_BUCKET_FMT         = "cloudoptix-tenant-configs-{account}"
STATE_BUCKET_FMT          = "cloudoptix-tenant-tfstate-{account}"

BUILD_TIMEOUT = 1800  # seconds to wait for a CodeBuild run (RDS create/destroy is very slow)
POLL_TIMEOUT  = 300   # seconds to wait for an async DynamoDB write (writer / parser / probe)
POLL_INTERVAL = 10


def log(stage, msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [{stage}] {msg}", flush=True)


def fail(msg):
    print(f"\n❌ FAIL: {msg}\n", flush=True)
    sys.exit(1)


class Harness:
    def __init__(self, args):
        self.session = boto3.Session(region_name=args.region)
        self.region = self.session.region_name
        if not self.region:
            fail("No region resolved. Pass --region or set AWS_DEFAULT_REGION.")

        self.account = self.session.client("sts").get_caller_identity()["Account"]
        self.scenario = args.scenario
        self.tenant_id = args.tenant_id or f"e2e-{uuid.uuid4().hex[:8]}"
        self.tenant_role_arn = args.tenant_role_arn
        self.external_id = args.external_id or "e2e-external-id"

        self.core_table = args.core_table
        self.codebuild_project = args.codebuild_project
        self.approve_lambda = args.approve_lambda
        self.tenant_mgmt_lambda = args.tenant_mgmt_lambda
        self.tf_upload_lambda = args.tf_upload_lambda
        self.config_bucket = args.config_bucket or CONFIG_BUCKET_FMT.format(account=self.account)
        self.state_bucket = args.state_bucket or STATE_BUCKET_FMT.format(account=self.account)
        self.config_key = f"{self.tenant_id}/main.tf"

        self.s3 = self.session.client("s3")
        self.table = self.session.resource("dynamodb").Table(self.core_table)
        self.sqs = self.session.client("sqs")
        self.cb = self.session.client("codebuild")
        self.lam = self.session.client("lambda")

        
        if self.scenario == "path-b" and args.tenant_id:
            prof = self.table.get_item(
                Key={"PK": f"TENANT#{self.tenant_id}", "SK": "PROFILE"}).get("Item") or {}
            self.tenant_role_arn = prof.get("TenantRoleArn", self.tenant_role_arn)
            if args.external_id:
                self.external_id = args.external_id
                self.table.update_item(
                    Key={"PK": f"TENANT#{self.tenant_id}", "SK": "PROFILE"},
                    UpdateExpression="SET ExternalId = :e",
                    ExpressionAttributeValues={":e": args.external_id})
                log("init", "using caller --external-id (synced into profile) for the tenant trust")
            else:
                self.external_id = prof.get("ExternalId", self.external_id)

        # The tenant session (verify/cleanup) is built lazily on first use — the register
        # stage never needs it, and the role can't be assumed until the CFN trust exists.
        self.tenant_profile = args.tenant_profile
        self._tenant_session_obj = None
        self.action_queue_url = self.sqs.get_queue_url(QueueName=args.action_queue_name)["QueueUrl"]

        # Scenario-specific shape.
        self.resource_id = None  # discovered from state after bootstrap
        self.negative = self.scenario == "instance-break"
        if self.scenario == "param":
            self.param_name = f"/cloudoptix/e2e/{self.tenant_id}"
            self.resource_id = self.param_name  # known up front
            self.tf_address = "aws_ssm_parameter.cloudoptix_e2e"
            self.resource_type = "parameter"
            self.edit_attr = "value"
            self.before, self.after = "before", "after"
        elif self.scenario == "path-b":
            # The tenant's "own" SSM parameter + resource label (as if from their TF).
            self.param_name = f"/cloudoptix/e2e/{self.tenant_id}"
            self.resource_id = self.param_name
            self.tf_address = "aws_ssm_parameter.user_param"
            self.resource_type = "parameter"
            self.edit_attr = "value"
            self.before, self.after = "before", "after"
        else:  # instance or instance-break (same bootstrap infra)
            self.tf_address = "aws_instance.cloudoptix_e2e"
            self.resource_type = "instance"
            self.edit_attr = "instance_type"
            self.before, self.after = "t3.small", "t3.micro"

        log("init", f"account={self.account} region={self.region} tenant={self.tenant_id} scenario={self.scenario}")
        log("init", f"config_bucket={self.config_bucket} state_bucket={self.state_bucket}")

    @property
    def tenant_session(self):
        if self._tenant_session_obj is None:
            self._tenant_session_obj = self._build_tenant_session()
        return self._tenant_session_obj

    def _build_tenant_session(self):
        if self.tenant_profile:
            log("init", f"tenant account via profile '{self.tenant_profile}'")
            return boto3.Session(profile_name=self.tenant_profile, region_name=self.region)
        log("init", "tenant account via assume-role on the platform session")
        params = {"RoleArn": self.tenant_role_arn, "RoleSessionName": "cloudoptix-e2e-verify"}
        if self.external_id:
            params["ExternalId"] = self.external_id
        creds = self.session.client("sts").assume_role(**params)["Credentials"]
        return boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=self.region,
        )

    
    def _providers_tf(self):
        ext = f'    external_id = "{self.external_id}"\n' if self.external_id else ''
        return (
            'terraform {\n  required_providers {\n    aws = {\n'
            '      source  = "hashicorp/aws"\n      version = "~> 5.0"\n    }\n  }\n}\n\n'
            'provider "aws" {\n'
            f'  region = "{self.region}"\n\n'
            '  assume_role {\n'
            f'    role_arn    = "{self.tenant_role_arn}"\n'
            f'{ext}'
            '  }\n}\n'
        )

    def _main_tf(self):
        if self.scenario == "param":
            return (
                'resource "aws_ssm_parameter" "cloudoptix_e2e" {\n'
                f'  name  = "{self.param_name}"\n'
                '  type  = "String"\n'
                f'  value = "{self.before}"\n'
                '}\n'
            )
        # instance scenario: a minimal internet-facing instance so L2 IGW->instance applies.
        tf = f'''data "aws_ami" "al2" {{
  most_recent = true
  owners      = ["amazon"]
  filter {{
    name   = "name"
    values = ["amzn2-ami-hvm-*-x86_64-gp2"]
  }}
}}

resource "aws_vpc" "cloudoptix_e2e" {{
  cidr_block = "10.0.0.0/16"
  tags = {{ Name = "cloudoptix-e2e" }}
}}

resource "aws_internet_gateway" "cloudoptix_e2e" {{
  vpc_id = aws_vpc.cloudoptix_e2e.id
}}

resource "aws_subnet" "cloudoptix_e2e" {{
  vpc_id                  = aws_vpc.cloudoptix_e2e.id
  cidr_block              = "10.0.1.0/24"
  map_public_ip_on_launch = true
}}

resource "aws_route_table" "cloudoptix_e2e" {{
  vpc_id = aws_vpc.cloudoptix_e2e.id
  route {{
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.cloudoptix_e2e.id
  }}
}}

resource "aws_route_table_association" "cloudoptix_e2e" {{
  subnet_id      = aws_subnet.cloudoptix_e2e.id
  route_table_id = aws_route_table.cloudoptix_e2e.id
}}

resource "aws_security_group" "cloudoptix_e2e" {{
  name_prefix = "cloudoptix-e2e-"
  vpc_id      = aws_vpc.cloudoptix_e2e.id
  ingress {{
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }}
  egress {{
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }}
}}

resource "aws_instance" "cloudoptix_e2e" {{
  ami                         = data.aws_ami.al2.id
  instance_type               = "{self.before}"
  subnet_id                   = aws_subnet.cloudoptix_e2e.id
  vpc_security_group_ids      = [aws_security_group.cloudoptix_e2e.id]
  associate_public_ip_address = true
  tags = {{ Name = "cloudoptix-e2e" }}
}}
'''
        if self.negative:
            # A second SG with NO ingress; the break finding swaps the instance onto this
            # so L2 (IGW->instance) finds it unreachable and the change auto-rolls-back.
            tf += (
                '\nresource "aws_security_group" "cloudoptix_e2e_deny" {\n'
                '  name_prefix = "cloudoptix-e2e-deny-"\n'
                '  vpc_id      = aws_vpc.cloudoptix_e2e.id\n'
                '  egress {\n'
                '    from_port   = 0\n'
                '    to_port     = 0\n'
                '    protocol    = "-1"\n'
                '    cidr_blocks = ["0.0.0.0/0"]\n'
                '  }\n'
                '}\n'
            )
        elif self.scenario == "instance-rds":
            # A private RDS whose SG allows inbound from the app instance's SG. The probe should
            # infer this dependency and prove instance -> RDS-ENI:5432 reachability.
            tf += (
                '\ndata "aws_availability_zones" "azs" {\n'
                '  state = "available"\n'
                '}\n\n'
                'resource "aws_subnet" "cloudoptix_e2e_db1" {\n'
                '  vpc_id            = aws_vpc.cloudoptix_e2e.id\n'
                '  cidr_block        = "10.0.2.0/24"\n'
                '  availability_zone = data.aws_availability_zones.azs.names[0]\n'
                '}\n\n'
                'resource "aws_subnet" "cloudoptix_e2e_db2" {\n'
                '  vpc_id            = aws_vpc.cloudoptix_e2e.id\n'
                '  cidr_block        = "10.0.3.0/24"\n'
                '  availability_zone = data.aws_availability_zones.azs.names[1]\n'
                '}\n\n'
                'resource "aws_db_subnet_group" "cloudoptix_e2e" {\n'
                '  name_prefix = "cloudoptix-e2e-"\n'
                '  subnet_ids  = [aws_subnet.cloudoptix_e2e_db1.id, aws_subnet.cloudoptix_e2e_db2.id]\n'
                '}\n\n'
                'resource "aws_security_group" "cloudoptix_e2e_db" {\n'
                '  name_prefix = "cloudoptix-e2e-db-"\n'
                '  vpc_id      = aws_vpc.cloudoptix_e2e.id\n'
                '  ingress {\n'
                '    from_port       = 5432\n'
                '    to_port         = 5432\n'
                '    protocol        = "tcp"\n'
                '    security_groups = [aws_security_group.cloudoptix_e2e.id]\n'
                '  }\n'
                '  egress {\n'
                '    from_port   = 0\n'
                '    to_port     = 0\n'
                '    protocol    = "-1"\n'
                '    cidr_blocks = ["0.0.0.0/0"]\n'
                '  }\n'
                '}\n\n'
                'resource "aws_db_instance" "cloudoptix_e2e" {\n'
                '  identifier_prefix      = "cloudoptix-e2e-"\n'
                '  engine                 = "postgres"\n'
                '  instance_class         = "db.t3.micro"\n'
                '  allocated_storage      = 20\n'
                '  username               = "cloudoptix"\n'
                '  password               = "Cloudoptix-E2E-Pw-123"\n'
                '  db_subnet_group_name   = aws_db_subnet_group.cloudoptix_e2e.name\n'
                '  vpc_security_group_ids = [aws_security_group.cloudoptix_e2e_db.id]\n'
                '  publicly_accessible    = false\n'
                '  skip_final_snapshot    = true\n'
                '}\n'
            )
        return tf

    def _put(self, bucket, key, body):
        self.s3.put_object(Bucket=bucket, Key=key, Body=body.encode("utf-8"))

    def seed_profile(self):
        self.table.put_item(Item={
            "PK": f"TENANT#{self.tenant_id}", "SK": "PROFILE", "Type": "TenantProfile",
            "TargetAccountId": self.account, "TargetRegion": self.region,
            "TenantRoleArn": self.tenant_role_arn, "ExternalId": self.external_id,
            "OnboardingPath": "A", "Status": "ONBOARDING",
            "CreatedAt": datetime.now(timezone.utc).isoformat(),
        })
        log("seed", "PROFILE written")

    def seed_workspace(self):
        self._put(self.config_bucket, f"{self.tenant_id}/providers.tf", self._providers_tf())
        self._put(self.config_bucket, self.config_key, self._main_tf())
        log("seed", f"workspace uploaded to s3://{self.config_bucket}/{self.tenant_id}/")

    # -- codebuild --------------------------------------------------------------
    def start_build(self, apply, action_id, resource_id):
        env = {
            "TENANT_ID": self.tenant_id, "ACTION_ID": action_id,
            "CONFIG_BUCKET": self.config_bucket, "STATE_BUCKET": self.state_bucket,
            "TENANT_ROLE_ARN": self.tenant_role_arn, "RESOURCE_ID": resource_id or "n/a",
            "AWS_REGION": self.region, "APPLY": "true" if apply else "false",
        }
        resp = self.cb.start_build(
            projectName=self.codebuild_project,
            environmentVariablesOverride=[{"name": k, "value": v, "type": "PLAINTEXT"} for k, v in env.items()],
        )
        bid = resp["build"]["id"]
        log("codebuild", f"started {'APPLY' if apply else 'PLAN'} build {bid} ({action_id})")
        return bid

    def wait_build(self, build_id, label="build"):
        deadline = time.time() + BUILD_TIMEOUT
        while time.time() < deadline:
            status = self.cb.batch_get_builds(ids=[build_id])["builds"][0]["buildStatus"]
            if status == "IN_PROGRESS":
                time.sleep(POLL_INTERVAL)
                continue
            log("codebuild", f"{label} {build_id} -> {status}")
            if status != "SUCCEEDED":
                fail(f"{label} {build_id} ended {status}. Check CodeBuild logs.")
            return status
        fail(f"{label} {build_id} timed out after {BUILD_TIMEOUT}s.")

    # -- polling helpers --------------------------------------------------------
    def get_finding(self, finding_id):
        return self.table.get_item(
            Key={"PK": f"TENANT#{self.tenant_id}", "SK": f"FINDING#{finding_id}"}
        ).get("Item")

    def wait_finding_status(self, finding_id, want, timeout=None):
        deadline = time.time() + (timeout or POLL_TIMEOUT)
        status = None
        while time.time() < deadline:
            item = self.get_finding(finding_id)
            status = item.get("Status") if item else None
            if status == want:
                log("poll", f"finding {finding_id} status == {want}")
                return item
            time.sleep(POLL_INTERVAL)
        fail(f"finding {finding_id} never reached status {want} (last={status}).")

    def await_state_mapping(self, tf_address=None):
        """Scans STATEADDR# items for a tf_address; returns the resource id."""
        tf_address = tf_address or self.tf_address
        deadline = time.time() + POLL_TIMEOUT
        while time.time() < deadline:
            kwargs = {"KeyConditionExpression": Key("PK").eq(f"TENANT#{self.tenant_id}")
                      & Key("SK").begins_with("STATEADDR#")}
            items = []
            while True:
                resp = self.table.query(**kwargs)
                items.extend(resp.get("Items", []))
                if "LastEvaluatedKey" not in resp:
                    break
                kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
            for it in items:
                if it.get("TerraformAddress") == tf_address:
                    rid = it["SK"].split("STATEADDR#", 1)[1]
                    log("poll", f"state_parser mapped {rid} -> {tf_address}")
                    return rid
            time.sleep(POLL_INTERVAL)
        fail(f"state_parser never mapped {tf_address} (is the tfstate S3 notification wired?).")

    # -- verification -----------------------------------------------------------
    def verify_resource(self, expected):
        if self.scenario in ("param", "path-b"):
            val = self.tenant_session.client("ssm").get_parameter(Name=self.resource_id)["Parameter"]["Value"]
            what = f"SSM {self.resource_id} value"
        else:
            resvs = self.tenant_session.client("ec2").describe_instances(
                InstanceIds=[self.resource_id])["Reservations"]
            val = resvs[0]["Instances"][0]["InstanceType"]
            what = f"instance {self.resource_id} type"
        log("verify", f"{what} = '{val}' (expected '{expected}')")
        if val != expected:
            fail(f"{what} '{val}' != expected '{expected}'")

    # -- findings / queue -------------------------------------------------------
    def seed_finding(self):
        finding_id = f"f-e2e-{uuid.uuid4().hex[:8]}"
        if self.negative:
            # In-place edit on the instance itself (so mode=inplace and the probe runs L2 on it):
            # swap its security group to the no-ingress "deny" SG, breaking reachability.
            edits = [{
                "edit_type": "update_attribute", "resource_address": "__TF_ADDRESS__",
                "attribute_path": "vpc_security_group_ids",
                "old_value": "[aws_security_group.cloudoptix_e2e.id]",
                "new_value": "[aws_security_group.cloudoptix_e2e_deny.id]",
                "full_resource_hcl": None,
            }]
            reasoning = "E2E negative: swap instance onto a no-ingress SG (breaks reachability)."
        else:
            edits = [{
                "edit_type": "update_attribute", "resource_address": "__TF_ADDRESS__",
                "attribute_path": self.edit_attr, "old_value": self.before,
                "new_value": self.after, "full_resource_hcl": None,
            }]
            reasoning = f"E2E synthetic finding: {self.edit_attr} {self.before} -> {self.after}."
        self.table.put_item(Item={
            "PK": f"TENANT#{self.tenant_id}", "SK": f"FINDING#{finding_id}",
            "ResourceId": self.resource_id, "ResourceType": self.resource_type,
            "Status": "NEW", "Action": "TIER_3_IAC", "Reasoning": reasoning,
            "EstimatedSavings": "0.00", "TerraformEdits": edits,
            "SystemTasks": [], "CreatedAt": datetime.now(timezone.utc).isoformat(),
        })
        log("seed", f"FINDING#{finding_id} written ({'SG-break' if self.negative else self.edit_attr})")
        return finding_id

    def send_action(self, finding_id):
        self.sqs.send_message(
            QueueUrl=self.action_queue_url,
            MessageBody=json.dumps({"tenant_id": self.tenant_id, "finding_id": finding_id,
                                    "resource_id": self.resource_id}),
            MessageGroupId=f"{self.tenant_id}-{self.resource_id}",
        )
        log("queue", f"action_queue message sent for {finding_id}")

    def invoke_approve(self, finding_id):
        event = {
            "routeKey": "POST /api/v1/tenants/{id}/recommendations/{rec_id}/approve",
            "rawPath": f"/api/v1/tenants/{self.tenant_id}/recommendations/{finding_id}/approve",
            "pathParameters": {"id": self.tenant_id, "rec_id": finding_id},
            "requestContext": {"http": {"method": "POST"},
                               "authorizer": {"jwt": {"claims": {"sub": "e2e-tester"}}}},
            "body": "{}",
        }
        resp = self.lam.invoke(FunctionName=self.approve_lambda, Payload=json.dumps(event).encode("utf-8"))
        payload = json.loads(resp["Payload"].read())
        log("approve", f"approve lambda -> {payload.get('statusCode')}: {payload.get('body')}")
        if payload.get("statusCode") != 200:
            fail(f"approve returned {payload.get('statusCode')}: {payload.get('body')}")

    # -- stages -----------------------------------------------------------------
    def bootstrap(self):
        if self.scenario == "path-b":
            return self._pathb_onboard()
        log("BOOTSTRAP", f"creating real {self.scenario} + state via CodeBuild apply")
        self.seed_profile()
        self.seed_workspace()
        self.wait_build(self.start_build(apply=True, action_id="bootstrap", resource_id=self.resource_id),
                        "bootstrap-apply")
        self.resource_id = self.await_state_mapping()
        self.verify_resource(self.before)
        log("BOOTSTRAP", "✅ resource created, tfstate parsed, STATEADDR# present")

    # -- Path B (register endpoint + tf_upload of existing files/state) ----------
    def pathb_register(self):
        if self.scenario != "path-b":
            fail("--stage register is only valid for --scenario path-b")
        tenant_acct = self.tenant_role_arn.split(":")[4]
        event = {
            "routeKey": "POST /api/v1/tenants/register",
            "rawPath": "/api/v1/tenants/register",
            "requestContext": {"http": {"method": "POST"},
                               "authorizer": {"jwt": {"claims": {"sub": "e2e-tester"}}}},
            "body": json.dumps({"account_id": tenant_acct, "region": self.region,
                                "has_terraform": True, "tenant_name": "e2e-path-b"}),
        }
        resp = self.lam.invoke(FunctionName=self.tenant_mgmt_lambda, Payload=json.dumps(event).encode("utf-8"))
        payload = json.loads(resp["Payload"].read())
        if payload.get("statusCode") != 201:
            fail(f"register returned {payload.get('statusCode')}: {payload.get('body')}")
        body = json.loads(payload["body"])
        if body.get("onboarding_path") != "B":
            fail(f"expected onboarding_path B, got {body.get('onboarding_path')}")
        role = body["cross_account_role"]
        tid = body["tenant_id"]
        print("\n" + "=" * 72)
        print(f"✅ Registered Path B tenant: {tid}")
        print(f"   role_arn    = {role['role_arn']}")
        print(f"   external_id = {role['external_id']}")
        print(f"   trusts      = {role['trusted_principal']}")
        print("\nNEXT: deploy/update the tenant CFN so that role trusts the platform principal")
        print(f"      with ExternalId = {role['external_id']}, then run:\n")
        print(f"  python e2e_pipeline_test.py --scenario path-b --tenant-id {tid} \\")
        print(f"    --tenant-role-arn {role['role_arn']} --stage all")
        print("=" * 72 + "\n")

    def _user_main_tf(self):
        return (
            'resource "aws_ssm_parameter" "user_param" {\n'
            f'  name  = "{self.param_name}"\n'
            '  type  = "String"\n'
            f'  value = "{self.before}"\n'
            '}\n'
        )

    def _pathb_onboard(self):
        if not self.external_id:
            fail("no ExternalId on the tenant profile — run '--stage register' first, then deploy the CFN.")
        log("PATH-B", f"onboarding tenant {self.tenant_id} using the registered external_id")

        # 1) Simulate the tenant's PRIOR terraform apply -> a real resource + a REAL tfstate.
        self._put(self.config_bucket, f"{self.tenant_id}/providers.tf", self._providers_tf())
        self._put(self.config_bucket, self.config_key, self._user_main_tf())
        self.wait_build(self.start_build(apply=True, action_id="user-apply", resource_id=self.resource_id),
                        "user-prior-apply")
        self.verify_resource(self.before)
        real_state = self.s3.get_object(
            Bucket=self.state_bucket, Key=f"{self.tenant_id}/terraform.tfstate")["Body"].read().decode("utf-8")
        log("PATH-B", "captured the tenant's real tfstate")

        # 2) Re-onboard via the real tf_upload endpoint: user main.tf + a BARE provider + their state.
        event = {
            "routeKey": "POST /api/v1/tenants/{id}/tf/upload",
            "rawPath": f"/api/v1/tenants/{self.tenant_id}/tf/upload",
            "pathParameters": {"id": self.tenant_id},
            "requestContext": {"http": {"method": "POST"},
                               "authorizer": {"jwt": {"claims": {"sub": "e2e-tester"}}}},
            "body": json.dumps({
                "files": [
                    {"path": "main.tf", "content": self._user_main_tf()},
                    {"path": "providers.tf", "content": f'provider "aws" {{\n  region = "{self.region}"\n}}\n'},
                ],
                "tfstate": real_state,
            }),
        }
        resp = self.lam.invoke(FunctionName=self.tf_upload_lambda, Payload=json.dumps(event).encode("utf-8"))
        payload = json.loads(resp["Payload"].read())
        log("PATH-B", f"tf_upload -> {payload.get('statusCode')}: {payload.get('body')}")
        if payload.get("statusCode") != 200:
            fail(f"tf_upload returned {payload.get('statusCode')}: {payload.get('body')}")

        # 3) Assert tf_upload injected the CloudOptix backend + an assume_role provider.
        providers = self.s3.get_object(
            Bucket=self.config_bucket, Key=f"{self.tenant_id}/providers.tf")["Body"].read().decode("utf-8")
        if "assume_role" not in providers:
            fail(f"tf_upload did not inject assume_role into providers.tf:\n{providers}")
        try:
            self.s3.get_object(Bucket=self.config_bucket, Key=f"{self.tenant_id}/backend.tf")
        except ClientError:
            fail("tf_upload did not write backend.tf")
        log("PATH-B", "✅ tf_upload stored files, injected assume_role + backend.tf")

        # 4) The uploaded tfstate should drive the state parser -> STATEADDR#.
        self.resource_id = self.await_state_mapping()
        log("PATH-B", "✅ uploaded state parsed -> STATEADDR#; workspace is CloudOptix-managed")

    def edit_flow(self):
        if self.resource_id is None:
            self.resource_id = self.await_state_mapping()
        if self.negative:
            return self._edit_flow_break()

        log("EDIT", "driving action_queue -> hcl_writer -> plan")
        finding_id = self.seed_finding()
        self.send_action(finding_id)
        item = self.wait_finding_status(finding_id, "PENDING_APPROVAL")

        edited = self.s3.get_object(Bucket=self.config_bucket, Key=self.config_key)["Body"].read().decode("utf-8")
        if item.get("SkippedResources"):
            log("EDIT", f"writer SkippedResources: {item['SkippedResources']}")
        # Whitespace-tolerant: aligned HCL puts many spaces before '=' (e.g. instance_type).
        if not re.search(rf'{re.escape(self.edit_attr)}\s*=\s*"{re.escape(self.after)}"', edited):
            log("EDIT", "main.tf after writer:\n" + edited.strip())
            fail("writer did NOT update main.tf (old value still present) -> hcl_writer/text-surgery issue.")
        log("EDIT", f"main.tf correctly rewritten ({self.edit_attr} -> \"{self.after}\") by the writer ✅")

        plan_build = item.get("CodeBuildPlanId")
        if plan_build:
            self.wait_build(plan_build, "writer-plan")
        self.verify_resource(self.before)  # plan only — real resource unchanged
        log("EDIT", "✅ writer edited main.tf, staged plan, PENDING_APPROVAL")

        log("APPROVE", "invoking approve -> CodeBuild apply")
        self.invoke_approve(finding_id)
        item = self.wait_finding_status(finding_id, "APPLYING")
        apply_build = item.get("CodeBuildApplyId")
        if not apply_build:
            fail("approve did not record CodeBuildApplyId.")
        self.wait_build(apply_build, "approve-apply")
        self.verify_resource(self.after)

        item = self.wait_finding_status(finding_id, "VALIDATED")
        detail = item.get("ValidationDetail", "")
        log("PROBE", f"post-apply validation detail: {detail}")
        if self.scenario == "instance-rds" and not re.search(r"instance->\S+: reachable", detail):
            fail(f"expected EC2->RDS reachability proof in validation detail; got: {detail!r}")
        log("APPROVE", f"✅ applied {self.before} -> {self.after}; finding VALIDATED")
        return finding_id

    def _edit_flow_break(self):
        log("BREAK", "seeding a finding that swaps the instance onto a no-ingress SG "
                     "(expect L2 fail + auto-rollback)")
        finding_id = self.seed_finding()
        self.send_action(finding_id)
        item = self.wait_finding_status(finding_id, "PENDING_APPROVAL")
        plan_build = item.get("CodeBuildPlanId")
        if plan_build:
            self.wait_build(plan_build, "writer-plan")

        log("APPROVE", "approving the breaking change -> apply (this SHOULD fail validation)")
        self.invoke_approve(finding_id)
        item = self.wait_finding_status(finding_id, "APPLYING")
        self.wait_build(item["CodeBuildApplyId"], "approve-apply")

        # probe L2 should now find IGW->instance unreachable and auto-roll-back.
        item = self.wait_finding_status(finding_id, "ROLLED_BACK", timeout=600)
        detail = item.get("ValidationDetail", "")
        log("PROBE", f"validation detail: {detail}")
        if "NOT reachable" not in detail:
            fail(f"expected L2 to report NOT reachable before rollback; got: {detail!r}")
        self._assert_instance_reachable_sg()
        log("ROLLBACK", "✅ L2 caught the break, finding ROLLED_BACK, instance SG restored")
        return finding_id

    def _assert_instance_reachable_sg(self):
        """After rollback the instance should be back on a SG that allows port-80 ingress."""
        ec2 = self.tenant_session.client("ec2")
        inst = ec2.describe_instances(InstanceIds=[self.resource_id])["Reservations"][0]["Instances"][0]
        sg_ids = [g["GroupId"] for g in inst.get("SecurityGroups", [])]
        sgs = ec2.describe_security_groups(GroupIds=sg_ids)["SecurityGroups"] if sg_ids else []
        has80 = any(
            p.get("FromPort") == 80 and any(r.get("CidrIp") == "0.0.0.0/0" for r in p.get("IpRanges", []))
            for sg in sgs for p in sg.get("IpPermissions", [])
        )
        log("verify", f"instance {self.resource_id} SGs={sg_ids} port-80 ingress restored = {has80}")
        if not has80:
            fail("rollback did not restore the instance's port-80 ingress SG")

    def cleanup(self):
        log("CLEANUP", "removing test resources and seeded data")
        if self.scenario in ("instance", "instance-break", "instance-rds"):
            # Tear down the real VPC/instance/etc. with a destroy apply (empty config).
            self._put(self.config_bucket, self.config_key,
                      "# CloudOptix E2E teardown — all managed resources removed.\n")
            try:
                bid = self.start_build(apply=True, action_id="destroy", resource_id=self.resource_id or "n/a")
                self.wait_build(bid, "destroy-apply")
            except SystemExit:
                log("cleanup", "⚠️  destroy apply FAILED — check the CodeBuild logs and remove the "
                               f"cloudoptix-e2e VPC/instance in the tenant account manually (tenant {self.tenant_id}).")
        elif self.scenario in ("param", "path-b") and self.resource_id:
            try:
                self.tenant_session.client("ssm").delete_parameter(Name=self.resource_id)
                log("cleanup", f"deleted SSM parameter {self.resource_id}")
            except ClientError as e:
                if e.response["Error"]["Code"] != "ParameterNotFound":
                    log("cleanup", f"warning: could not delete parameter: {e}")

        for bucket in (self.config_bucket, self.state_bucket):
            try:
                objs = self.s3.list_objects_v2(Bucket=bucket, Prefix=f"{self.tenant_id}/").get("Contents", [])
                if objs:
                    self.s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": o["Key"]} for o in objs]})
                    log("cleanup", f"deleted {len(objs)} objects from {bucket}")
            except ClientError as e:
                log("cleanup", f"warning: S3 cleanup on {bucket}: {e}")

        items, kwargs = [], {"KeyConditionExpression": Key("PK").eq(f"TENANT#{self.tenant_id}")}
        while True:
            resp = self.table.query(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        with self.table.batch_writer() as batch:
            for it in items:
                batch.delete_item(Key={"PK": it["PK"], "SK": it["SK"]})
        log("cleanup", f"deleted {len(items)} DynamoDB items")
        log("CLEANUP", "✅ done")


def main():
    p = argparse.ArgumentParser(description="CloudOptix end-to-end pipeline test")
    p.add_argument("--tenant-role-arn", required=True,
                   help="Tenant deployment role ARN assumed by the provider / probe")
    p.add_argument("--scenario", choices=["param", "instance", "instance-break", "instance-rds", "path-b"],
                   default="param",
                   help="param: SSM value flip (fast). instance: real EC2 downsize exercising L2 reachability. "
                        "instance-break: remove the SG ingress so L2 fails and the change auto-rolls-back. "
                        "instance-rds: EC2 + private RDS; assert L2 proves EC2->RDS reachability (SLOW ~30-40 min). "
                        "path-b: register via the API, then upload existing TF + state via tf_upload and manage it. "
                        "Run --stage register first, deploy the CFN with the printed external_id, then --stage all.")
    p.add_argument("--tenant-mgmt-lambda", default=TENANT_MGMT_LAMBDA_DEFAULT)
    p.add_argument("--tf-upload-lambda", default=TF_UPLOAD_LAMBDA_DEFAULT)
    p.add_argument("--external-id", default=None,
                   help="External ID required by the tenant role's trust policy (omit if none)")
    p.add_argument("--tenant-profile", default=None,
                   help="AWS profile for the tenant account (else assume --tenant-role-arn from the platform session)")
    p.add_argument("--region", default=None, help="AWS region (default: env/profile)")
    p.add_argument("--tenant-id", default=None, help="Override the generated test tenant id")
    p.add_argument("--core-table", default=CORE_TABLE_DEFAULT)
    p.add_argument("--config-bucket", default=None)
    p.add_argument("--state-bucket", default=None)
    p.add_argument("--action-queue-name", default=ACTION_QUEUE_NAME_DEFAULT)
    p.add_argument("--codebuild-project", default=CODEBUILD_PROJECT_DEFAULT)
    p.add_argument("--approve-lambda", default=APPROVE_LAMBDA_DEFAULT)
    p.add_argument("--stage", choices=["all", "register", "bootstrap", "edit", "cleanup"], default="all")
    p.add_argument("--keep", action="store_true", help="Skip cleanup at the end")
    args = p.parse_args()

    h = Harness(args)
    if args.stage == "register":
        h.pathb_register()
        return
    try:
        if args.stage in ("all", "bootstrap"):
            h.bootstrap()
        if args.stage in ("all", "edit"):
            h.edit_flow()
        if args.stage == "cleanup":
            h.cleanup()
            return
        print("\n✅ END-TO-END PIPELINE TEST PASSED\n")
    finally:
        if args.stage == "all" and not args.keep:
            h.cleanup()


if __name__ == "__main__":
    main()
