terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.40"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }

  # Remote state -- an S3 bucket + DynamoDB lock table created once, out of
  # band (e.g. via `terraform/bootstrap/`, not included here to keep this
  # environment self-contained to review). Uncomment and fill in before
  # running `terraform init` against a real account:
  #
  # backend "s3" {
  #   bucket         = "fraud-detection-platform-tfstate"
  #   key            = "prod/terraform.tfstate"
  #   region         = "us-east-1"
  #   dynamodb_table = "fraud-detection-platform-tflock"
  #   encrypt        = true
  # }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "fraud-detection-platform"
      Environment = "prod"
      ManagedBy   = "terraform"
    }
  }
}
