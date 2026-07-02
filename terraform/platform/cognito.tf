resource "aws_cognito_user_pool" "tenant_pool" {
  name = "cloudoptix-tenant"
  password_policy {
    minimum_length    = 8
    require_lowercase = true
    require_numbers   = true
    require_symbols   = true
    require_uppercase = true
  }

  admin_create_user_config {
    allow_admin_create_user_only = false
  }

  # Auto-confirm new signups (no email code) via the PreSignUp trigger.
  lambda_config {
    pre_sign_up = aws_lambda_function.cognito_presignup.arn
  }
}

resource "aws_iam_role" "presignup_role" {
  name = "CloudOptix-Cognito-PreSignUp-Role"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy_attachment" "presignup_logs" {
  role       = aws_iam_role.presignup_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_lambda_function" "cognito_presignup" {
  filename         = data.archive_file.backend_zip.output_path
  source_code_hash = data.archive_file.backend_zip.output_base64sha256

  function_name = "CloudOptix-Cognito-PreSignUp"
  role          = aws_iam_role.presignup_role.arn
  handler       = "lambdas.cognito_presignup.handler.lambda_handler"
  runtime       = "python3.11"
  timeout       = 10
}

resource "aws_lambda_permission" "cognito_invoke_presignup" {
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.cognito_presignup.function_name
  principal     = "cognito-idp.amazonaws.com"
  source_arn    = aws_cognito_user_pool.tenant_pool.arn
}

resource "aws_cognito_user_pool_client" "frontend_client" {
  name                = "cloudoptix-frontend-client"
  user_pool_id        = aws_cognito_user_pool.tenant_pool.id
  generate_secret     = false
  explicit_auth_flows = ["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
}

