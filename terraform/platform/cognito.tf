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
}

resource "aws_cognito_user_pool_client" "frontend_client" {
    name = "cloudoptix-frontend-client"
    user_pool_id = aws_cognito_user_pool.tenant_pool.id
    generate_secret = false
    explicit_auth_flows = ["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
}

