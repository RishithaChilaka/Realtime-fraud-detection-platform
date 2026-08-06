variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "name_prefix" {
  type    = string
  default = "fraud-detection"
}

variable "domain_name" {
  description = "Base domain for public services, e.g. fraud.example.com -> api.fraud.example.com, review.fraud.example.com, ..."
  type        = string
}

variable "acm_certificate_arn" {
  description = "ACM cert ARN covering *.<domain_name>, in the same region as aws_region (ALB is regional, not CloudFront)"
  type        = string
}

variable "route53_zone_id" {
  description = "Optional: hosted zone ID to auto-create DNS records in. Leave null and point your own DNS at the ALB's DNS name instead."
  type        = string
  default     = null
}

variable "github_repository" {
  description = "GitHub org/repo for the OIDC deploy role trust policy"
  type        = string
  default     = "RishithaChilaka/Realtime-fraud-detection-platform"
}

variable "image_tag" {
  description = "Image tag for the initial task definitions Terraform provisions. CI/CD (deploy.yml) manages subsequent deploys -- see the ecs module's lifecycle.ignore_changes note."
  type        = string
  default     = "latest"
}

variable "single_nat_gateway" {
  type    = bool
  default = true
}

variable "rds_multi_az" {
  type    = bool
  default = true
}

variable "redis_num_nodes" {
  type    = number
  default = 2
}

variable "msk_broker_count" {
  type    = number
  default = 2
}
