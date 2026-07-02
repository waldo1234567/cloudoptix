resource "aws_s3_bucket" "artifacts" {
  bucket = "cloudoptix-artifacts-${data.aws_caller_identity.current.account_id}"
}

# Public bucket for onboarding assets the tenant's browser/CloudFormation must
# fetch cross-account (the tenant-role CFN template for one-click Launch Stack).
resource "aws_s3_bucket" "public_assets" {
  bucket = "cloudoptix-public-assets-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "public_assets_block" {
  bucket                  = aws_s3_bucket.public_assets.id
  block_public_acls       = false
  block_public_policy     = false
  ignore_public_acls      = false
  restrict_public_buckets = false
}

resource "aws_s3_bucket_policy" "public_assets_policy" {
  bucket     = aws_s3_bucket.public_assets.id
  depends_on = [aws_s3_bucket_public_access_block.public_assets_block]
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "PublicReadOnboardingAssets"
      Effect    = "Allow"
      Principal = "*"
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.public_assets.arn}/*"
    }]
  })
}

resource "aws_s3_object" "tenant_role_template" {
  bucket       = aws_s3_bucket.public_assets.id
  key          = "cloudformation/tenant-role.yaml"
  source       = "${path.module}/cloudformation/tenant-role.yaml"
  etag         = filemd5("${path.module}/cloudformation/tenant-role.yaml")
  content_type = "text/yaml"
}

locals {
  tenant_role_template_url = "https://${aws_s3_bucket.public_assets.bucket}.s3.${var.aws_region}.amazonaws.com/${aws_s3_object.tenant_role_template.key}"
}

resource "aws_s3_bucket_public_access_block" "artifacts_block" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}


resource "aws_s3_bucket" "tenant_configs" {
  bucket = "cloudoptix-tenant-configs-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_versioning" "config_versioning" {
  bucket = aws_s3_bucket.tenant_configs.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket" "tenant_tfstate" {
  bucket = "cloudoptix-tenant-tfstate-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "configs_block" {
  bucket                  = aws_s3_bucket.tenant_configs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_public_access_block" "tfstate_block" {
  bucket                  = aws_s3_bucket.tenant_tfstate.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}