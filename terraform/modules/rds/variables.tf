variable "name_prefix" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "instance_class" {
  type    = string
  default = "db.t4g.medium"
}

variable "allocated_storage_gb" {
  type    = number
  default = 50
}

variable "max_allocated_storage_gb" {
  type    = number
  default = 200
}

variable "db_name" {
  type    = string
  default = "fraud_detection"
}

variable "db_username" {
  type    = string
  default = "fraud_admin"
}

variable "db_password" {
  description = "Pulled from aws_secretsmanager_secret_version in environments/prod/main.tf, never hardcoded"
  type        = string
  sensitive   = true
}

variable "multi_az" {
  type    = bool
  default = true
}

variable "backup_retention_days" {
  type    = number
  default = 7
}

variable "deletion_protection" {
  type    = bool
  default = true
}

variable "tags" {
  type    = map(string)
  default = {}
}
