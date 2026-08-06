output "cluster_name" {
  value = aws_ecs_cluster.this.name
}

output "cluster_arn" {
  value = aws_ecs_cluster.this.arn
}

output "alb_dns_name" {
  value = aws_lb.this.dns_name
}

output "alb_security_group_id" {
  value = aws_security_group.alb.id
}

output "task_security_group_ids" {
  value = { for k, sg in aws_security_group.task : k => sg.id }
}

output "public_service_urls" {
  value = { for k, v in local.public_services : k => "https://${v.public_subdomain}.${var.domain_name}" }
}
