output "dynamodb_table_name" {
  value = aws_dynamodb_table.cloudoptix_table.name
}

output "scan_queue_url" {
  value = aws_sqs_queue.scan_queue.url
}

output "action_queue_url" {
  value = aws_sqs_queue.action_queue.url
}

output "api_gateway_id" {
  value = aws_apigatewayv2_api.http_api.id
}

output "cognito_user_pool_id" {
  value = aws_cognito_user_pool.tenant_pool.id
}

output "artifacts_bucket_name" {
  value = aws_s3_bucket.artifacts.id
}