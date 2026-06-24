resource "aws_apigatewayv2_api" "http_api" {
  name = "cloudoptix-http-api"
  protocol_type = "HTTP"
  cors_configuration {
    allow_origins = ["*"]
    allow_methods = ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
    allow_headers = ["Content-Type", "Authorization"]
  }
}

resource "aws_apigatewayv2_stage" "default_stage" {
  api_id      = aws_apigatewayv2_api.http_api.id
  name        = "$default"
  auto_deploy = true
}

resource "aws_apigatewayv2_authorizer" "cognito_jwt" {
  api_id = aws_apigatewayv2_api.http_api.id
  authorizer_type = "JWT"
  identity_sources = ["$request.header.Authorization"]
  name = "cognito-authorizer"

    jwt_configuration {
      audience = [aws_cognito_user_pool_client.frontend_client.id]
      issuer = "https://cognito-idp.${var.aws_region}.amazonaws.com/${aws_cognito_user_pool.tenant_pool.id}"
    }
}

resource "aws_apigatewayv2_integration" "compiler_integration" {
  api_id = aws_apigatewayv2_api.http_api.id
  integration_type = "AWS_PROXY"
  integration_uri  = aws_lambda_function.compiler_api.invoke_arn
  integration_method = "POST"
}

resource "aws_apigatewayv2_route" "compiler_route" {
  api_id    = aws_apigatewayv2_api.http_api.id
  route_key = "POST /api/v1/compile"
  
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito_jwt.id
  target             = "integrations/${aws_apigatewayv2_integration.compiler_integration.id}"
}

resource "aws_lambda_permission" "apigw_compiler" {
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.compiler_api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http_api.execution_arn}/*/*/api/v1/compile"
}


resource "aws_apigatewayv2_integration" "runner_integration" {
  api_id           = aws_apigatewayv2_api.http_api.id
  integration_type = "AWS_PROXY"
  integration_uri  = aws_lambda_function.iac_runner.invoke_arn
  integration_method = "POST"
}

resource "aws_apigatewayv2_route" "runner_route" {
  api_id    = aws_apigatewayv2_api.http_api.id
  route_key = "POST /api/v1/execute"
  
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito_jwt.id
  target             = "integrations/${aws_apigatewayv2_integration.runner_integration.id}"
}

resource "aws_lambda_permission" "apigw_runner" {
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.iac_runner.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http_api.execution_arn}/*/*/api/v1/execute"
}