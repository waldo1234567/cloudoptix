terraform {
  cloud {
    organization = "test-terraform-waldo"

    workspaces {
      name = "cloudoptix-platform"
    }
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}





