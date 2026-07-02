# CloudOptix Backend

[![Backend CI/CD](https://github.com/waldo1234567/cloudoptix/actions/workflows/backend.yml/badge.svg)](https://github.com/waldo1234567/cloudoptix/actions/workflows/backend.yml)

CloudOptix is a Terraform first AWS cost optimizer. It scans a customer AWS account, finds cost saving opportunities on the resources that Terraform manages, writes the change directly into the customer's Terraform, and applies it through a controlled pipeline with validation and automatic rollback.

The core idea is that **Terraform state is the single source of truth**. CloudOptix only proposes and applies changes to resources that already live in Terraform state, because those are the only resources it can safely modify through `terraform apply`.

## How it works

The platform is an event driven pipeline of AWS Lambda functions connected by SQS queues, DynamoDB, and CodeBuild.

### Discovery and recommendation flow

1. **Scheduler** enqueues a scan for each tenant on an interval (also triggered manually from the UI).
2. **Scanner** assumes the tenant's cross account role, calls the AWS describe APIs, and keeps only the resources whose id appears in the tenant's Terraform state. Resources found in state but no longer present in AWS are flagged as `DRIFTED`.
3. **Graph builder** builds a dependency graph so the rules engine can assess blast radius before touching anything.
4. **Metrics collector** pulls CloudWatch utilization for each resource.
5. **Rules engine** evaluates each resource and, when a saving is worthwhile and safe, emits a finding with structured Terraform edit descriptors.
6. **HCL Writer** applies those edits to the tenant's `main.tf` as a text surgery (no `python-hcl2`, just precise pattern replace), then triggers a Terraform plan.

### Approval and apply flow

7. **Build Monitor** watches CodeBuild completions through EventBridge and moves the finding through its status state machine.
8. The user reviews the plan and the version diff in the UI, then approves.
9. **Approve** starts a Terraform apply through CodeBuild.
10. **Probe** runs after a successful apply as a post apply validator. It checks the resource is healthy and reachable (VPC Reachability Analyzer for network paths, including east west EC2 to RDS).
11. If validation fails, CloudOptix reverts `main.tf` to the previous S3 version and re applies, so a bad change is automatically rolled back.

### Finding status state machine

```
NEW -> PLANNING -> PENDING_APPROVAL | PLAN_FAILED
    -> APPLYING -> VALIDATING | APPLY_FAILED
    -> VALIDATED | VALIDATION_FAILED
    -> ROLLING_BACK -> ROLLED_BACK | ROLLBACK_FAILED
```

## Onboarding (bring your own Terraform)

Onboarding is a single path. A tenant brings an existing Terraform configuration and its state.

1. **Register** creates the tenant profile and generates a unique external id.
2. The tenant deploys a **CloudFormation stack** (one click launch URL, external id prefilled) that creates the cross account IAM role `CloudOptix-Tenant-Deployment-Role`, trusting the platform account.
3. **Verify access** confirms the platform can assume that role.
4. **TF Upload** stores the tenant's `.tf` files and `terraform.tfstate`. State is required, otherwise a plan would try to recreate resources that already exist. CloudOptix owns the backend (injected by the buildspec) and adds a provider `assume_role` override. All CloudOptix authored files are namespaced with a `cloudoptix_` prefix so they never collide with the tenant's files.

## Cross account credential model

The platform account holds the control plane (Lambda, API, state buckets). The tenant account holds the real resources and the deployment role.

CodeBuild runs as the platform runner role, which owns the S3 Terraform backend. The `aws` provider inside the tenant workspace uses `assume_role` to operate in the tenant account. Tenant credentials are never exported globally in the build, because the Terraform S3 backend authenticates from the ambient environment before the provider config is read.

## Drift and reconcile

Because Terraform state is the source of truth, CloudOptix treats a resource deleted outside Terraform (for example in the AWS console) as drift.

* The scanner marks such a resource `DRIFTED` (in state, not in AWS).
* The **Reconcile** action starts a CodeBuild run of `terraform apply -refresh-only`, which syncs the state to reality. The updated state write triggers the state parser to reconcile the address map, then a follow up scan removes the stale resource from the inventory.
* Note: refresh only removes the resource from state, but if it is still declared in the tenant's config a future apply would recreate it. This is inherent to Terraform and is surfaced to the user.

## AWS resources used

* **AWS Lambda**: the whole pipeline (about twenty one functions, see below).
* **API Gateway (HTTP API v2)**: the REST surface, protected by a **Cognito** JWT authorizer.
* **Amazon Cognito**: user pool and app client for auth, plus a PreSignUp trigger that auto confirms signups.
* **DynamoDB**: a single table `cloudoptix-core-table` holds every tenant item (profile, findings, resources, state address map, execution history).
* **Amazon S3**: `tenant-configs` (versioned Terraform workspaces), `tenant-tfstate` (state files), `public-assets` (the onboarding CloudFormation template), `artifacts`.
* **Amazon SQS (FIFO)**: scan, graph, metrics, rules, and action queues connect the pipeline stages.
* **AWS CodeBuild**: the Terraform runner that runs init, plan, apply, refresh only, and rollback.
* **Amazon EventBridge**: routes CodeBuild state changes to the Build Monitor.
* **Amazon SNS**: operator notifications.
* **IAM**: separate roles for the API lambdas, the worker lambdas, CodeBuild, and the Cognito trigger.

## Lambda functions

| Function | Role |
| --- | --- |
| Scheduler | Enqueues scans per tenant |
| Scanner | Discovers AWS resources, filters to Terraform managed, flags drift |
| State Parser | Parses `terraform.tfstate` into an id to address map, reconciles it |
| Graph Builder | Builds the dependency graph |
| Metrics Collector | Pulls CloudWatch utilization |
| Rules Engine | Produces cost findings and Terraform edit descriptors |
| HCL Writer | Applies text edits to `main.tf` and starts a plan |
| Build Monitor | Reconciles finding status from CodeBuild outcomes |
| Probe Executor | Post apply validation and automatic rollback |
| Tenant Mgmt | Registration and cross account role instructions |
| TF Upload | Stores tenant Terraform and state |
| API Recommendations | Lists findings |
| API Approve | Starts the apply build |
| API Finding Status | Single finding status for polling |
| API Workspace | Serves versioned Terraform files for the diff view |
| API Resources | Lists discovered resources and scan status |
| API Scan | Manual scan trigger |
| API Reconcile | Starts the refresh only build |
| API Verify Access | Assumes the tenant role to confirm access |
| API Delete | Deletes a tenant, a finding, or a resource |
| Cognito PreSignUp | Auto confirms new signups |

## API endpoints

All routes sit behind the Cognito JWT authorizer.

```
POST   /api/v1/tenants/register
POST   /api/v1/tenants/{id}/tf/upload
POST   /api/v1/tenants/{id}/verify-access
POST   /api/v1/tenants/{id}/scan
POST   /api/v1/tenants/{id}/reconcile
GET    /api/v1/tenants/{id}/resources
GET    /api/v1/tenants/{id}/recommendations
GET    /api/v1/tenants/{id}/findings/{rec_id}/status
POST   /api/v1/tenants/{id}/recommendations/{rec_id}/approve
GET    /api/v1/tenants/{id}/workspace
GET    /api/v1/tenants/{id}/workspace/{file}
DELETE /api/v1/tenants/{id}
DELETE /api/v1/tenants/{id}/recommendations/{rec_id}
DELETE /api/v1/tenants/{id}/resources?resource_id=...
```

## Data model

Single DynamoDB table, partitioned per tenant.

| PK | SK | Purpose |
| --- | --- | --- |
| `TENANT#{id}` | `PROFILE` | Tenant config, cross account role, scan and reconcile status |
| `TENANT#{id}` | `FINDING#{id}` | A recommendation and its Terraform edits |
| `TENANT#{id}` | `RESOURCE#{aws_id}` | A discovered resource, status `NEW` or `DRIFTED` |
| `TENANT#{id}` | `STATEADDR#{aws_id}` | Maps an AWS id to its Terraform address |
| `TENANT#{id}` | `EXEC#{ts}#{finding}` | Execution history |

## Layout

```
terraform/platform/
  *.tf                 Infrastructure (API, Cognito, DynamoDB, S3, SQS, CodeBuild, IAM)
  buildspec.yml        The CodeBuild Terraform runner (plan, apply, refresh only, rollback)
  src/
    lambdas/           All Lambda handlers
    rules/             Rules engine and resource models
cloudformation/
  tenant-role.yaml     Cross account role template the tenant deploys
e2e_pipeline_test.py   End to end integration test against real AWS
```

## Deploy

```
cd terraform/platform
terraform init
terraform apply
```

The Lambda code is packaged from `src/` into a single archive, so a single apply updates every function.

## Continuous delivery

`.github/workflows/backend.yml` runs the backend through GitHub Actions.

* **On every pull request**: compile the Lambda sources, check Terraform formatting, run `terraform validate`, and post a `terraform plan` as a comment on the pull request.
* **On merge to `main`**: run `terraform apply`, gated behind a manual approval.

State and AWS credentials live in Terraform Cloud (remote execution), so the pipeline only needs one secret and no AWS keys are stored in GitHub.

One time setup:

1. Add a repository secret `TF_API_TOKEN`, a Terraform Cloud API token.
2. Create a GitHub Environment named `production` and add yourself as a required reviewer. That reviewer prompt is the approval gate before apply.
3. Keep the Terraform Cloud workspace in the CLI or API driven run mode so the pipeline can drive the runs.

## Testing

`e2e_pipeline_test.py` drives the deployed pipeline against a real SSM parameter or EC2 instance in a test tenant account, seeding synthetic findings to exercise the writer, plan, approve, apply, and validation stages. Run it with platform account credentials.
