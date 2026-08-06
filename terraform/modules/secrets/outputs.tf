output "secret_arn" {
  value = aws_secretsmanager_secret.app.arn
}

output "secret_id" {
  value = aws_secretsmanager_secret.app.id
}

output "postgres_password" {
  value     = random_password.db.result
  sensitive = true
}

output "redis_auth_token" {
  value     = random_password.redis_auth_token.result
  sensitive = true
}

output "jwt_secret_key" {
  value     = random_password.jwt_secret.result
  sensitive = true
}
