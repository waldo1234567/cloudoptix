resource "aws_s3_bucket" "artifacts" {
  bucket = "cloudoptix-artifacts-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "artifacts_block" {
  bucket = aws_s3_bucket.artifacts.id
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