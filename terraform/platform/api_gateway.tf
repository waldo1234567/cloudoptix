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

resource "aws_apigatewayv2_integration" "recommendations_integration" {
  api_id             = aws_apigatewayv2_api.http_api.id
  integration_type   = "AWS_PROXY"
  integration_uri    = aws_lambda_function.api_recommendations.invoke_arn
  integration_method = "POST"
}

resource "aws_apigatewayv2_route" "recommendations_route" {
  api_id    = aws_apigatewayv2_api.http_api.id
  route_key = "GET /api/v1/tenants/{id}/recommendations"

  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito_jwt.id
  target             = "integrations/${aws_apigatewayv2_integration.recommendations_integration.id}"
}

resource "aws_lambda_permission" "apigw_recommendations" {
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api_recommendations.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http_api.execution_arn}/*/*/api/v1/tenants/*/recommendations"
}

resource "aws_apigatewayv2_integration" "approve_integration" {
  api_id             = aws_apigatewayv2_api.http_api.id
  integration_type   = "AWS_PROXY"
  integration_uri    = aws_lambda_function.api_approve.invoke_arn
  integration_method = "POST"
}

resource "aws_apigatewayv2_route" "approve_route" {
  api_id    = aws_apigatewayv2_api.http_api.id
  route_key = "POST /api/v1/tenants/{id}/recommendations/{rec_id}/approve"

  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito_jwt.id
  target             = "integrations/${aws_apigatewayv2_integration.approve_integration.id}"
}

resource "aws_lambda_permission" "apigw_approve" {
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api_approve.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http_api.execution_arn}/*/*/api/v1/tenants/*/recommendations/*/approve"
}


resource "aws_apigatewayv2_integration" "tenant_mgmt_integration" {
  api_id             = aws_apigatewayv2_api.http_api.id
  integration_type   = "AWS_PROXY"
  integration_uri    = aws_lambda_function.tenant_mgmt.invoke_arn
  integration_method = "POST"
}

resource "aws_apigatewayv2_route" "tenant_register_route" {
  api_id    = aws_apigatewayv2_api.http_api.id
  route_key = "POST /api/v1/tenants/register"

  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito_jwt.id
  target             = "integrations/${aws_apigatewayv2_integration.tenant_mgmt_integration.id}"
}

resource "aws_apigatewayv2_integration" "tf_upload_integration" {
  api_id             = aws_apigatewayv2_api.http_api.id
  integration_type   = "AWS_PROXY"
  integration_uri    = aws_lambda_function.tf_upload.invoke_arn
  integration_method = "POST"
}

resource "aws_apigatewayv2_route" "tenant_tf_upload_route" {
  api_id    = aws_apigatewayv2_api.http_api.id
  route_key = "POST /api/v1/tenants/{id}/tf/upload"

  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito_jwt.id
  target             = "integrations/${aws_apigatewayv2_integration.tf_upload_integration.id}"
}

resource "aws_lambda_permission" "apigw_tf_upload" {
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.tf_upload.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http_api.execution_arn}/*/*/api/v1/tenants/*/tf/upload"
}

resource "aws_lambda_permission" "apigw_tenant_mgmt" {
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.tenant_mgmt.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http_api.execution_arn}/*/*/api/v1/tenants/*"
}

