resource "aws_sqs_queue" "scan_queue" {
  name                        = "cloudoptix-scan-queue.fifo"
  fifo_queue                  = true
  content_based_deduplication = true
  visibility_timeout_seconds  = 300
}

resource "aws_sqs_queue" "action_queue" {
  name                        = "cloudoptix-action-queue.fifo"
  fifo_queue                  = true
  content_based_deduplication = true
  visibility_timeout_seconds  = 300
}

resource "aws_sqs_queue" "graph_queue" {
  name                       = "CloudOptix-Graph-Queue.fifo"
  fifo_queue                 = true
  visibility_timeout_seconds = 300
}

resource "aws_sqs_queue" "metrics_queue" {
  name                       = "CloudOptix-Metrics-Queue.fifo"
  fifo_queue                 = true
  visibility_timeout_seconds = 300
}

resource "aws_sqs_queue" "rules_queue" {
  name                       = "CloudOptix-Rules-Queue.fifo"
  fifo_queue                 = true
  visibility_timeout_seconds = 300
}
