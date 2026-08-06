variable "name_prefix" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "public_subnet_ids" {
  type = list(string)
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "execution_role_arn" {
  type = string
}

variable "task_role_arn" {
  type = string
}

variable "acm_certificate_arn" {
  description = "ACM cert (must cover *.<domain_name>) for the ALB's HTTPS listener"
  type        = string
}

variable "domain_name" {
  description = "Base domain; each public service gets <key>.<domain_name>, e.g. api.fraud.example.com"
  type        = string
}

variable "route53_zone_id" {
  description = "Hosted zone to create alias records in. Leave null to skip DNS record creation (use the ALB's own DNS name instead)."
  type        = string
  default     = null
}

# Each entry becomes one ECS Fargate task definition + service.
# `public_subdomain` = null means "internal only, no ALB target group"
# (producer/consumer/scheduler-type services); non-null registers an ALB
# listener rule routing https://<public_subdomain>.<domain_name> to it.
variable "services" {
  type = map(object({
    image             = string
    cpu               = number
    memory            = number
    port              = optional(number)
    public_subdomain  = optional(string)
    health_check_path = optional(string, "/health")
    environment       = optional(map(string), {})
    secrets           = optional(map(string), {}) # env var name -> "arn:...:secret:...:json-key::"
    desired_count     = optional(number, 1)
    command           = optional(list(string))
  }))
}

variable "tags" {
  type    = map(string)
  default = {}
}
