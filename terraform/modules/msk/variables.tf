variable "name_prefix" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "kafka_version" {
  type    = string
  default = "3.6.0"
}

variable "broker_count" {
  description = "Must be a multiple of the number of AZs used (private_subnet_ids length)"
  type        = number
  default     = 2
}

variable "broker_instance_type" {
  type    = string
  default = "kafka.m5.large"
}

variable "broker_ebs_volume_gb" {
  type    = number
  default = 200
}

variable "cloudwatch_log_group_name" {
  type = string
}

variable "tags" {
  type    = map(string)
  default = {}
}
