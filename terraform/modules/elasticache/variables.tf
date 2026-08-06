variable "name_prefix" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "node_type" {
  type    = string
  default = "cache.t4g.small"
}

variable "num_cache_clusters" {
  description = "1 = single node (dev/staging), 2+ = primary + replica with automatic failover"
  type        = number
  default     = 2
}

variable "auth_token" {
  description = "Pulled from aws_secretsmanager_secret_version, min 16 chars per AWS requirements"
  type        = string
  sensitive   = true
}

variable "tags" {
  type    = map(string)
  default = {}
}
