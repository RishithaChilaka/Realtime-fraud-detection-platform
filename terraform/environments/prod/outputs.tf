output "alb_dns_name" {
  description = "Point your DNS here if route53_zone_id wasn't set (CNAME each subdomain to this)"
  value       = module.ecs.alb_dns_name
}

output "public_service_urls" {
  value = module.ecs.public_service_urls
}

output "ecs_cluster_name" {
  value = module.ecs.cluster_name
}

output "ecr_repository_urls" {
  value = module.ecr.repository_urls
}

output "rds_app_endpoint" {
  value = module.rds_app.endpoint
}

output "redis_primary_endpoint" {
  value     = module.elasticache.primary_endpoint
  sensitive = true
}

output "msk_bootstrap_brokers_tls" {
  value     = module.msk.bootstrap_brokers_tls
  sensitive = true
}

output "mlflow_artifacts_bucket" {
  value = aws_s3_bucket.mlflow_artifacts.bucket
}

output "github_deploy_role_arn" {
  description = "Set as AWS_DEPLOY_ROLE_ARN in the GitHub repo's Actions secrets/variables for .github/workflows/deploy.yml"
  value       = module.iam.github_deploy_role_arn
}

output "app_secrets_arn" {
  value = module.secrets.secret_arn
}
