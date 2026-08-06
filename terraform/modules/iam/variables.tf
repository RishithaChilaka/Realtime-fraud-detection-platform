variable "name_prefix" {
  type = string
}

variable "secrets_arn" {
  type = string
}

variable "mlflow_artifacts_bucket_arn" {
  type = string
}

variable "github_repository" {
  description = "e.g. RishithaChilaka/Realtime-fraud-detection-platform"
  type        = string
}

variable "tags" {
  type    = map(string)
  default = {}
}
