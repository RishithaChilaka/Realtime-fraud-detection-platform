# Generates and stores every credential the platform needs in AWS Secrets
# Manager -- nothing sensitive is ever a literal in a .tf/.tfvars file or
# a docker-compose environment block. `src/common/secrets.py` reads these
# at application startup when `USE_AWS_SECRETS_MANAGER=true` (see that
# module and README's security section).
#
# One secret ID (`<name_prefix>/app`) holding a JSON blob rather than one
# secret per credential -- fewer Secrets Manager API calls at cold start
# (one GetSecretValue instead of five), and it matches
# `Settings.aws_secrets_manager_secret_id`'s single-secret-id shape.

resource "random_password" "db" {
  length  = 32
  special = false # RDS password character restrictions
}

resource "random_password" "redis_auth_token" {
  length  = 32
  special = false # ElastiCache AUTH token restrictions
}

resource "random_password" "jwt_secret" {
  length  = 64
  special = true
}

resource "random_password" "airflow_webserver_secret" {
  length  = 32
  special = false
}

resource "random_password" "grafana_admin" {
  length  = 20
  special = false
}

resource "aws_secretsmanager_secret" "app" {
  name                    = "${var.name_prefix}/app"
  description             = "Fraud detection platform application secrets (DB, Redis, JWT, Grafana, Airflow)"
  recovery_window_in_days = var.recovery_window_days
  kms_key_id              = var.kms_key_id
  tags                    = var.tags
}

resource "aws_secretsmanager_secret_version" "app" {
  secret_id = aws_secretsmanager_secret.app.id
  secret_string = jsonencode({
    postgres_password       = random_password.db.result
    redis_auth_token         = random_password.redis_auth_token.result
    jwt_secret_key           = random_password.jwt_secret.result
    airflow_webserver_secret = random_password.airflow_webserver_secret.result
    grafana_admin_password   = random_password.grafana_admin.result
  })
}
