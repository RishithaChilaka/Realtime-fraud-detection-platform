variable "name_prefix" {
  type = string
}

variable "kms_key_id" {
  description = "KMS key for Secrets Manager encryption; null uses the default aws/secretsmanager key"
  type        = string
  default     = null
}

variable "recovery_window_days" {
  type    = number
  default = 7
}

variable "tags" {
  type    = map(string)
  default = {}
}
