resource "aws_iam_role" "worker_lambda_role" {
  name = "CloudOptix-Pipeline-Worker-Role"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "worker_lambda_permissions" {
  name = "Pipeline-Worker-Policy"
  role = aws_iam_role.worker_lambda_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:Query", "dynamodb:Scan", "dynamodb:BatchWriteItem"]
        Effect = "Allow"
        Resource = [
          aws_dynamodb_table.cloudoptix_table.arn,
          "${aws_dynamodb_table.cloudoptix_table.arn}/index/*",
          aws_dynamodb_table.recommendations.arn
        ]
      },
      {
        Action   = "sts:AssumeRole"
        Effect   = "Allow"
        Resource = "arn:aws:iam::*:role/CloudOptix-Tenant-Deployment-Role"
      },
      {
        Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:SendMessage"]
        Effect = "Allow"
        Resource = [
          aws_sqs_queue.scan_queue.arn,
          aws_sqs_queue.action_queue.arn,
          aws_sqs_queue.graph_queue.arn,
          aws_sqs_queue.metrics_queue.arn
        ]
      },
      {
        Action   = "sns:Publish"
        Effect   = "Allow"
        Resource = aws_sns_topic.alerts.arn
      },
      {
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Effect   = "Allow"
        Resource = "arn:aws:logs:*:*:*"
      }
    ]
  })
}

resource "aws_lambda_function" "scanner" {
  filename         = data.archive_file.backend_zip.output_path
  source_code_hash = data.archive_file.backend_zip.output_base64sha256
  function_name    = "Cloudoptix-Scanner"
  role             = aws_iam_role.worker_lambda_role.arn
  handler          = "lambdas.scanner.main.lambda_handler"
  runtime          = "python3.11"
  timeout          = 300

  environment {
    variables = {
      DYNAMODB_TABLE_NAME = aws_dynamodb_table.cloudoptix_table.name
      GRAPH_QUEUE_URL     = aws_sqs_queue.graph_queue.url
    }
  }
}

resource "aws_lambda_function" "graph_builder" {
  filename         = data.archive_file.backend_zip.output_path
  source_code_hash = data.archive_file.backend_zip.output_base64sha256
  function_name    = "CloudOptix-Graph-Builder"
  role             = aws_iam_role.worker_lambda_role.arn
  handler          = "lambdas.graph.builder.lambda_handler"
  runtime          = "python3.11"
  timeout          = 300

  environment {
    variables = {
      DYNAMODB_TABLE_NAME = aws_dynamodb_table.cloudoptix_table.name
      METRICS_QUEUE_URL   = aws_sqs_queue.metrics_queue.url
    }
  }
}

resource "aws_lambda_function" "metrics_collector" {
  filename         = data.archive_file.backend_zip.output_path
  source_code_hash = data.archive_file.backend_zip.output_base64sha256
  function_name    = "CloudOptix-Metrics-Collector"
  role             = aws_iam_role.worker_lambda_role.arn
  handler          = "lambdas.metrics.collector.lambda_handler"
  runtime          = "python3.11"
  timeout          = 300
  environment {
    variables = {
      DYNAMODB_TABLE_NAME = aws_dynamodb_table.cloudoptix_table.name
      RULES_QUEUE_URL     = aws_sqs_queue.rules_queue.url
    }
  }
}


resource "aws_lambda_function" "rules_engine" {
  filename         = data.archive_file.backend_zip.output_path
  source_code_hash = data.archive_file.backend_zip.output_base64sha256
  function_name    = "CloudOptix-Rules-Engine"
  role             = aws_iam_role.worker_lambda_role.arn
  handler          = "rules.engine.lambda_handler"
  runtime          = "python3.11"
  timeout          = 300

  environment {
    variables = {
      DYNAMODB_TABLE_NAME = aws_dynamodb_table.cloudoptix_table.name
      ACTION_QUEUE_URL    = aws_sqs_queue.action_queue.url
    }
  }

}

resource "aws_lambda_function" "probe_executor" {
  filename         = data.archive_file.backend_zip.output_path
  source_code_hash = data.archive_file.backend_zip.output_base64sha256
  function_name    = "CloudOptix-Probe-Executor"
  role             = aws_iam_role.worker_lambda_role.arn
  handler          = "lambdas.probe.handler.lambda_handler"
  runtime          = "python3.11"
  timeout          = 300

  environment {
    variables = {
      DYNAMODB_TABLE_NAME = aws_dynamodb_table.cloudoptix_table.name
      SNS_TOPIC_ARN       = aws_sns_topic.alerts.arn
    }
  }
}

resource "aws_lambda_function" "scheduler" {
  filename         = data.archive_file.backend_zip.output_path
  source_code_hash = data.archive_file.backend_zip.output_base64sha256
  function_name    = "CloudOptix-Scheduler"
  role             = aws_iam_role.worker_lambda_role.arn
  handler          = "lambdas.scheduler.main.lambda_handler"
  runtime          = "python3.11"
  timeout          = 60

  environment {
    variables = {
      SCAN_QUEUE_URL      = aws_sqs_queue.scan_queue.url
      ACTION_QUEUE_URL    = aws_sqs_queue.action_queue.url
      DYNAMODB_TABLE_NAME = aws_dynamodb_table.cloudoptix_table.name
    }
  }
}

resource "aws_lambda_event_source_mapping" "scanner_sqs_trigger" {
  event_source_arn = aws_sqs_queue.scan_queue.arn
  function_name    = aws_lambda_function.scanner.arn
  batch_size       = 1
}

resource "aws_lambda_event_source_mapping" "graph_trigger" {
  event_source_arn = aws_sqs_queue.graph_queue.arn
  function_name    = aws_lambda_function.graph_builder.arn
  batch_size       = 1
}

resource "aws_lambda_event_source_mapping" "metrics_trigger" {
  event_source_arn = aws_sqs_queue.metrics_queue.arn
  function_name    = aws_lambda_function.metrics_collector.arn
  batch_size       = 1
}

resource "aws_lambda_event_source_mapping" "rules_trigger" {
  event_source_arn = aws_sqs_queue.rules_queue.arn
  function_name    = aws_lambda_function.rules_engine.arn 
  batch_size       = 1
}

resource "aws_iam_role_policy_attachment" "action_sqs_trigger" {
  role       = aws_iam_role.worker_lambda_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaSQSQueueExecutionRole"
}

resource "aws_lambda_event_source_mapping" "action_queue_trigger" {
  event_source_arn = aws_sqs_queue.action_queue.arn
  function_name    = aws_lambda_function.probe_executor.arn
  batch_size       = 1

  depends_on = [aws_iam_role_policy_attachment.action_sqs_trigger]
}

resource "aws_cloudwatch_event_rule" "daily_orchestration" {
  name                = "CloudOptix-Daily-Orchestration"
  description         = "Triggers the CloudOptix Scheduler to queue tenant scans"
  schedule_expression = "cron(0 0 * * ? *)"
}

resource "aws_cloudwatch_event_target" "trigger_scheduler" {
  rule      = aws_cloudwatch_event_rule.daily_orchestration.name
  target_id = "TriggerCloudOptixScheduler"
  arn       = aws_lambda_function.scheduler.arn
}

resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.scheduler.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.daily_orchestration.arn
}
