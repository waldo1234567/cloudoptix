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
        Action = ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:BatchGetItem"]
        Effect = "Allow"
        Resource = [
          aws_dynamodb_table.recommendations.arn,
          aws_dynamodb_table.execution_history.arn
        ]
      },
      {
        Action   = "codebuild:StartBuild"
        Effect   = "Allow"
        Resource = aws_codebuild_project.terraform_runner.arn
      },
      {
        Action   = ["s3:PutObject", "s3:GetObject"]
        Effect   = "Allow"
        Resource = "${aws_s3_bucket.tenant_configs.arn}/*"
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

resource "aws_lambda_function" "compiler_api" {
  filename         = data.archive_file.backend_zip.output_path
  source_code_hash = data.archive_file.backend_zip.output_base64sha256

  function_name = "CloudOptix-Compiler-API"
  role          = aws_iam_role.api_lambda_role.arn
  handler       = "lambdas.api.compiler.lambda_handler"
  runtime       = "python3.11"
  timeout       = 30

  environment {
    variables = {
      RECOMMENDATIONS_TABLE = aws_dynamodb_table.recommendations.name
    }
  }
}


resource "aws_lambda_function" "iac_runner" {
  filename         = data.archive_file.backend_zip.output_path
  source_code_hash = data.archive_file.backend_zip.output_base64sha256
  handler          = "lambdas.executor.iac_runner.lambda_handler"
  function_name    = "CloudOptix-IaC-Runner"
  role             = aws_iam_role.api_lambda_role.arn
  runtime          = "python3.11"
  timeout          = 30

  environment {
    variables = {
      CONFIG_BUCKET     = aws_s3_bucket.tenant_configs.bucket
      STATE_BUCKET      = aws_s3_bucket.tenant_tfstate.bucket
      CODEBUILD_PROJECT = aws_codebuild_project.terraform_runner.name
      HISTORY_TABLE     = aws_dynamodb_table.execution_history.name
    }
  }
}
