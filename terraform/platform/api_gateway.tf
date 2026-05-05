resource "aws_apigatewayv2_api" "http_api" {
  name = "cloudoptix-http-api"
  protocol_type = "HTTP"
  cors_configuration {
    allow_origins = ["*"]
    allow_methods = ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
    allow_headers = ["Content-Type", "Authorization"]
  }
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

