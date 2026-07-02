resource "aws_iam_role" "codebuild_runner" {
  name = "CloudOptix-CodeBuild-Runner-Role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "codebuild.amazonaws.com" }
    }]
  })
}


resource "aws_iam_role_policy" "codebuild_permissions" {
  name = "CodeBuild-Execution-Policy"
  role = aws_iam_role.codebuild_runner.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
        Effect = "Allow"
        Resource = [
          aws_s3_bucket.tenant_configs.arn,
          "${aws_s3_bucket.tenant_configs.arn}/*",
          aws_s3_bucket.tenant_tfstate.arn,
          "${aws_s3_bucket.tenant_tfstate.arn}/*"
        ]
      },
      {
        Action   = "sts:AssumeRole"
        Effect   = "Allow"
        Resource = "arn:aws:iam::*:role/CloudOptix-Tenant-Deployment-Role"
      },
      {
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Effect   = "Allow"
        Resource = "*"
      }
    ]
  })
}

resource "aws_codebuild_project" "terraform_runner" {
  name          = "CloudOptix-Terraform-Runner"
  description   = "Ephemeral container to execute tenant Terraform configurations autonomously"
  build_timeout = "60"
  service_role  = aws_iam_role.codebuild_runner.arn

  artifacts {
    type = "NO_ARTIFACTS"
  }

  environment {
    compute_type                = "BUILD_GENERAL1_SMALL"
    image                       = "aws/codebuild/amazonlinux2-x86_64-standard:5.0"
    type                        = "LINUX_CONTAINER"
    image_pull_credentials_type = "CODEBUILD"
  }


  source {
    type      = "NO_SOURCE"
    buildspec = file("${path.module}/buildspec.yml")
  }
}

resource "aws_iam_role" "api_lambda_role" {
  name = "CloudOptix-API-Lambda-Role"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "api_lambda_permissions" {
  name = "Lambda-API-Policy"
  role = aws_iam_role.api_lambda_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:Query", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:BatchWriteItem"]
        Effect = "Allow"
        Resource = [
          aws_dynamodb_table.recommendations.arn,
          aws_dynamodb_table.execution_history.arn,
          aws_dynamodb_table.cloudoptix_table.arn
        ]
      },
      {
        Action   = "codebuild:StartBuild"
        Effect   = "Allow"
        Resource = aws_codebuild_project.terraform_runner.arn
      },
      {
        Action   = "sts:AssumeRole"
        Effect   = "Allow"
        Resource = "arn:aws:iam::*:role/CloudOptix-Tenant-Deployment-Role"
      },
      {
        Action   = "sqs:SendMessage"
        Effect   = "Allow"
        Resource = aws_sqs_queue.scan_queue.arn
      },
      {
        Action = ["s3:PutObject", "s3:GetObject", "s3:GetObjectVersion", "s3:DeleteObject", "s3:DeleteObjectVersion"]
        Effect = "Allow"
        Resource = [
          "${aws_s3_bucket.tenant_configs.arn}/*",
          "${aws_s3_bucket.tenant_tfstate.arn}/*"
        ]
      },
      {
        Action = ["s3:ListBucket", "s3:ListBucketVersions"]
        Effect = "Allow"
        Resource = [
          aws_s3_bucket.tenant_configs.arn,
          aws_s3_bucket.tenant_tfstate.arn
        ]
      },
      {
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Effect   = "Allow"
        Resource = "arn:aws:logs:*:*:*"
      }
    ]
  })
}

data "archive_file" "backend_zip" {
  type        = "zip"
  source_dir  = "${path.module}/src"
  output_path = "${path.module}/backend_payload.zip"
}

resource "aws_lambda_function" "api_recommendations" {
  filename         = data.archive_file.backend_zip.output_path
  source_code_hash = data.archive_file.backend_zip.output_base64sha256

  function_name = "CloudOptix-API-Recommendations"
  role          = aws_iam_role.api_lambda_role.arn
  handler       = "lambdas.api.recommendations.lambda_handler"
  runtime       = "python3.11"
  timeout       = 30

  environment {
    variables = {
      DYNAMODB_TABLE_NAME = aws_dynamodb_table.cloudoptix_table.name
    }
  }
}

resource "aws_lambda_function" "api_approve" {
  filename         = data.archive_file.backend_zip.output_path
  source_code_hash = data.archive_file.backend_zip.output_base64sha256

  function_name = "CloudOptix-API-Approve"
  role          = aws_iam_role.api_lambda_role.arn
  handler       = "lambdas.api.approve.lambda_handler"
  runtime       = "python3.11"
  timeout       = 30

  environment {
    variables = {
      DYNAMODB_TABLE_NAME    = aws_dynamodb_table.cloudoptix_table.name
      CONFIG_BUCKET          = aws_s3_bucket.tenant_configs.bucket
      STATE_BUCKET           = aws_s3_bucket.tenant_tfstate.bucket
      CODEBUILD_PROJECT_NAME = aws_codebuild_project.terraform_runner.name
    }
  }
}

resource "aws_lambda_function" "api_workspace" {
  filename         = data.archive_file.backend_zip.output_path
  source_code_hash = data.archive_file.backend_zip.output_base64sha256

  function_name = "CloudOptix-API-Workspace"
  role          = aws_iam_role.api_lambda_role.arn
  handler       = "lambdas.api.workspace.lambda_handler"
  runtime       = "python3.11"
  timeout       = 30

  environment {
    variables = {
      CONFIG_BUCKET = aws_s3_bucket.tenant_configs.bucket
    }
  }
}

resource "aws_lambda_function" "api_finding_status" {
  filename         = data.archive_file.backend_zip.output_path
  source_code_hash = data.archive_file.backend_zip.output_base64sha256

  function_name = "CloudOptix-API-Finding-Status"
  role          = aws_iam_role.api_lambda_role.arn
  handler       = "lambdas.api.finding_status.lambda_handler"
  runtime       = "python3.11"
  timeout       = 30

  environment {
    variables = {
      DYNAMODB_TABLE_NAME = aws_dynamodb_table.cloudoptix_table.name
    }
  }
}


resource "aws_lambda_function" "tenant_mgmt" {
  filename         = data.archive_file.backend_zip.output_path
  source_code_hash = data.archive_file.backend_zip.output_base64sha256

  function_name = "CloudOptix-Tenant-Mgmt"
  role          = aws_iam_role.api_lambda_role.arn
  handler       = "lambdas.tenant_mgmt.handler.lambda_handler"
  runtime       = "python3.11"
  timeout       = 30

  environment {
    variables = {
      DYNAMODB_TABLE_NAME = aws_dynamodb_table.cloudoptix_table.name
      CONFIG_BUCKET       = aws_s3_bucket.tenant_configs.bucket
      STATE_BUCKET        = aws_s3_bucket.tenant_tfstate.bucket
      PLATFORM_ACCOUNT_ID = data.aws_caller_identity.current.account_id
      CFN_TEMPLATE_URL    = local.tenant_role_template_url
    }
  }
}

resource "aws_lambda_function" "api_reconcile" {
  filename         = data.archive_file.backend_zip.output_path
  source_code_hash = data.archive_file.backend_zip.output_base64sha256

  function_name = "CloudOptix-API-Reconcile"
  role          = aws_iam_role.api_lambda_role.arn
  handler       = "lambdas.api.reconcile.lambda_handler"
  runtime       = "python3.11"
  timeout       = 30

  environment {
    variables = {
      DYNAMODB_TABLE_NAME    = aws_dynamodb_table.cloudoptix_table.name
      CONFIG_BUCKET          = aws_s3_bucket.tenant_configs.bucket
      STATE_BUCKET           = aws_s3_bucket.tenant_tfstate.bucket
      CODEBUILD_PROJECT_NAME = aws_codebuild_project.terraform_runner.name
    }
  }
}

resource "aws_lambda_function" "api_delete" {
  filename         = data.archive_file.backend_zip.output_path
  source_code_hash = data.archive_file.backend_zip.output_base64sha256

  function_name = "CloudOptix-API-Delete"
  role          = aws_iam_role.api_lambda_role.arn
  handler       = "lambdas.api.delete.lambda_handler"
  runtime       = "python3.11"
  timeout       = 60

  environment {
    variables = {
      DYNAMODB_TABLE_NAME = aws_dynamodb_table.cloudoptix_table.name
      CONFIG_BUCKET       = aws_s3_bucket.tenant_configs.bucket
      STATE_BUCKET        = aws_s3_bucket.tenant_tfstate.bucket
    }
  }
}

resource "aws_lambda_function" "api_scan" {
  filename         = data.archive_file.backend_zip.output_path
  source_code_hash = data.archive_file.backend_zip.output_base64sha256

  function_name = "CloudOptix-API-Scan"
  role          = aws_iam_role.api_lambda_role.arn
  handler       = "lambdas.api.scan.lambda_handler"
  runtime       = "python3.11"
  timeout       = 30

  environment {
    variables = {
      DYNAMODB_TABLE_NAME = aws_dynamodb_table.cloudoptix_table.name
      SCAN_QUEUE_URL      = aws_sqs_queue.scan_queue.url
    }
  }
}

resource "aws_lambda_function" "api_resources" {
  filename         = data.archive_file.backend_zip.output_path
  source_code_hash = data.archive_file.backend_zip.output_base64sha256

  function_name = "CloudOptix-API-Resources"
  role          = aws_iam_role.api_lambda_role.arn
  handler       = "lambdas.api.resources.lambda_handler"
  runtime       = "python3.11"
  timeout       = 30

  environment {
    variables = {
      DYNAMODB_TABLE_NAME = aws_dynamodb_table.cloudoptix_table.name
    }
  }
}

resource "aws_lambda_function" "api_verify_access" {
  filename         = data.archive_file.backend_zip.output_path
  source_code_hash = data.archive_file.backend_zip.output_base64sha256

  function_name = "CloudOptix-API-Verify-Access"
  role          = aws_iam_role.api_lambda_role.arn
  handler       = "lambdas.api.verify_access.lambda_handler"
  runtime       = "python3.11"
  timeout       = 30

  environment {
    variables = {
      DYNAMODB_TABLE_NAME = aws_dynamodb_table.cloudoptix_table.name
    }
  }
}

resource "aws_lambda_function" "tf_upload" {
  filename         = data.archive_file.backend_zip.output_path
  source_code_hash = data.archive_file.backend_zip.output_base64sha256

  function_name = "CloudOptix-TF-Upload"
  role          = aws_iam_role.api_lambda_role.arn
  handler       = "lambdas.tf_upload.handler.lambda_handler"
  runtime       = "python3.11"
  timeout       = 30

  environment {
    variables = {
      DYNAMODB_TABLE_NAME = aws_dynamodb_table.cloudoptix_table.name
      CONFIG_BUCKET       = aws_s3_bucket.tenant_configs.bucket
      STATE_BUCKET        = aws_s3_bucket.tenant_tfstate.bucket
      PLATFORM_ACCOUNT_ID = data.aws_caller_identity.current.account_id
    }
  }
}

