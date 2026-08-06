# Wires every module together into one deployable environment. Mirrors
# docker-compose.yml's service topology as closely as ECS Fargate's model
# allows -- see the `ecs` module's docstring for the one deliberate
# divergence (Spark/consumer running single-task instead of clustered).

locals {
  name_prefix = var.name_prefix
  tags        = { Project = "fraud-detection-platform" }
}

# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------

module "vpc" {
  source             = "../../modules/vpc"
  name_prefix        = local.name_prefix
  single_nat_gateway = var.single_nat_gateway
  tags               = local.tags
}

# ---------------------------------------------------------------------------
# Container registry
# ---------------------------------------------------------------------------

module "ecr" {
  source      = "../../modules/ecr"
  name_prefix = local.name_prefix
  tags        = local.tags
}

# ---------------------------------------------------------------------------
# S3 (MLflow artifact store) -- encrypted at rest (SSE-KMS), versioned,
# blocked from any public access. MLflow's tracking server (running as an
# ECS service, see local.services.mlflow below) points its
# --default-artifact-root at this bucket instead of docker-compose's local
# volume.
# ---------------------------------------------------------------------------

resource "aws_kms_key" "s3" {
  description             = "${local.name_prefix} S3 encryption at rest"
  deletion_window_in_days = 7
  enable_key_rotation     = true
  tags                    = local.tags
}

resource "aws_s3_bucket" "mlflow_artifacts" {
  bucket = "${local.name_prefix}-mlflow-artifacts-${data.aws_caller_identity.current.account_id}"
  tags   = local.tags
}

resource "aws_s3_bucket_versioning" "mlflow_artifacts" {
  bucket = aws_s3_bucket.mlflow_artifacts.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "mlflow_artifacts" {
  bucket = aws_s3_bucket.mlflow_artifacts.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.s3.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "mlflow_artifacts" {
  bucket                  = aws_s3_bucket.mlflow_artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

data "aws_caller_identity" "current" {}

# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

module "secrets" {
  source      = "../../modules/secrets"
  name_prefix = local.name_prefix
  tags        = local.tags
}

# ---------------------------------------------------------------------------
# IAM (ECS roles + GitHub OIDC deploy role)
# ---------------------------------------------------------------------------

module "iam" {
  source                      = "../../modules/iam"
  name_prefix                 = local.name_prefix
  secrets_arn                 = module.secrets.secret_arn
  mlflow_artifacts_bucket_arn = aws_s3_bucket.mlflow_artifacts.arn
  github_repository            = var.github_repository
  tags                        = local.tags
}

# ---------------------------------------------------------------------------
# Data stores
# ---------------------------------------------------------------------------

module "rds_app" {
  source              = "../../modules/rds"
  name_prefix         = "${local.name_prefix}-app"
  vpc_id              = module.vpc.vpc_id
  private_subnet_ids  = module.vpc.private_subnet_ids
  db_name             = "fraud_detection"
  db_password         = module.secrets.postgres_password
  multi_az            = var.rds_multi_az
  tags                = local.tags
  # Ingress (ECS tasks -> 5432) is added below via aws_security_group_rule,
  # not passed into this module -- see modules/rds/main.tf's comment on why
  # inline ingress blocks aren't mixed with standalone security group rules.
}

module "rds_airflow" {
  source                   = "../../modules/rds"
  name_prefix              = "${local.name_prefix}-airflow"
  vpc_id                   = module.vpc.vpc_id
  private_subnet_ids       = module.vpc.private_subnet_ids
  instance_class           = "db.t4g.small"
  allocated_storage_gb     = 20
  max_allocated_storage_gb = 50
  db_name                  = "airflow"
  db_username              = "airflow"
  db_password              = module.secrets.postgres_password # separate RDS instance, same generated password for simplicity; use a second random_password in modules/secrets for stricter separation
  multi_az                 = false
  deletion_protection      = false
  tags                     = local.tags
}

module "elasticache" {
  source              = "../../modules/elasticache"
  name_prefix         = local.name_prefix
  vpc_id              = module.vpc.vpc_id
  private_subnet_ids  = module.vpc.private_subnet_ids
  num_cache_clusters  = var.redis_num_nodes
  auth_token          = module.secrets.redis_auth_token
  tags                = local.tags
}

resource "aws_cloudwatch_log_group" "msk" {
  name              = "/msk/${local.name_prefix}"
  retention_in_days = 30
  tags              = local.tags
}

module "msk" {
  source                     = "../../modules/msk"
  name_prefix                = local.name_prefix
  vpc_id                     = module.vpc.vpc_id
  private_subnet_ids         = module.vpc.private_subnet_ids
  broker_count               = var.msk_broker_count
  cloudwatch_log_group_name  = aws_cloudwatch_log_group.msk.name
  tags                       = local.tags
}

# ---------------------------------------------------------------------------
# ECS Fargate services
# ---------------------------------------------------------------------------

locals {
  common_env = {
    KAFKA_BOOTSTRAP_SERVERS   = module.msk.bootstrap_brokers_tls
    KAFKA_TOPIC_TRANSACTIONS  = "transactions.raw"
    KAFKA_TOPIC_DLQ           = "transactions.dlq"
    POSTGRES_HOST             = module.rds_app.endpoint
    POSTGRES_PORT             = tostring(module.rds_app.port)
    POSTGRES_DB               = module.rds_app.db_name
    POSTGRES_USER             = "fraud_admin"
    POSTGRES_SSLMODE          = "require" # RDS enforces this server-side too (rds.force_ssl=1)
    REDIS_HOST                = module.elasticache.primary_endpoint
    REDIS_PORT                = tostring(module.elasticache.port)
    REDIS_USE_TLS             = "true" # ElastiCache transit_encryption_mode = "required"
    MLFLOW_TRACKING_URI       = "https://mlflow.${var.domain_name}"
    MLFLOW_EXPERIMENT_NAME    = "fraud-detection"
    MLFLOW_XGBOOST_MODEL_NAME = "fraud_xgboost"
    MLFLOW_LIGHTGBM_MODEL_NAME = "fraud_lightgbm"
    MLFLOW_ACTIVE_MODEL_NAME  = "fraud_xgboost"
    PUSHGATEWAY_URL           = "pushgateway.${local.name_prefix}.internal:9091"
    USE_AWS_SECRETS_MANAGER   = "true"
    AWS_SECRETS_MANAGER_SECRET_ID = module.secrets.secret_id
    AWS_REGION                = var.aws_region
  }

  # ECS injects these into the container's env at start (valueFrom a
  # Secrets Manager ARN + JSON key). Belt-and-suspenders with
  # USE_AWS_SECRETS_MANAGER=true above: if ECS's injection ever produced
  # a stale/empty value for one of these three, src/common/secrets.py's
  # startup fetch (same secret, same keys) overrides it rather than the
  # app silently starting with a blank credential -- see that module's
  # docstring for the full reasoning.
  common_secrets = {
    POSTGRES_PASSWORD = "${module.secrets.secret_arn}:postgres_password::"
    REDIS_AUTH_TOKEN  = "${module.secrets.secret_arn}:redis_auth_token::"
    JWT_SECRET_KEY    = "${module.secrets.secret_arn}:jwt_secret_key::"
  }

  ecr_urls = module.ecr.repository_urls

  services = {
    api = {
      image             = "${local.ecr_urls["api"]}:${var.image_tag}"
      cpu               = 1024
      memory            = 2048
      port              = 8080
      public_subdomain  = "api"
      health_check_path = "/health"
      environment       = merge(local.common_env, { API_HOST = "0.0.0.0", API_PORT = "8080" })
      secrets           = local.common_secrets
      desired_count     = 2
    }
    review-ui = {
      image             = "${local.ecr_urls["streamlit"]}:${var.image_tag}"
      cpu               = 512
      memory            = 1024
      port              = 8501
      public_subdomain  = "review"
      health_check_path = "/"
      environment       = merge(local.common_env, { FRAUD_API_BASE_URL = "https://api.${var.domain_name}" })
      desired_count     = 1
    }
    producer = {
      image         = "${local.ecr_urls["producer"]}:${var.image_tag}"
      cpu           = 256
      memory        = 512
      port          = 8000
      environment   = merge(local.common_env, { PRODUCER_TPS = "1000", PRODUCER_EDGE_CASE_RATIO = "0.05" })
      desired_count = 1
    }
    consumer = {
      # See this file's header comment + the ecs module's docstring: single
      # Fargate task in local[*] mode, not a clustered Spark deployment.
      image         = "${local.ecr_urls["consumer"]}:${var.image_tag}"
      cpu           = 2048
      memory        = 8192
      port          = 8000
      environment   = merge(local.common_env, { SPARK_MASTER_URL = "local[*]" })
      desired_count = 1
    }
    mlflow = {
      image             = "ghcr.io/mlflow/mlflow:v2.11.3"
      cpu               = 512
      memory            = 1024
      port              = 5000
      public_subdomain  = "mlflow"
      health_check_path = "/"
      command = [
        "mlflow", "server", "--host", "0.0.0.0", "--port", "5000",
        "--backend-store-uri", "sqlite:////tmp/mlflow.db",
        "--default-artifact-root", "s3://${aws_s3_bucket.mlflow_artifacts.bucket}/artifacts",
      ]
      environment   = { AWS_REGION = var.aws_region }
      desired_count = 1
    }
    grafana = {
      image             = "grafana/grafana:10.4.2"
      cpu               = 256
      memory            = 512
      port              = 3000
      public_subdomain  = "grafana"
      health_check_path = "/api/health"
      environment       = { GF_SECURITY_ADMIN_USER = "admin" }
      secrets           = { GF_SECURITY_ADMIN_PASSWORD = "${module.secrets.secret_arn}:grafana_admin_password::" }
      desired_count     = 1
    }
    prometheus = {
      image         = "prom/prometheus:v2.51.2"
      cpu           = 512
      memory        = 1024
      port          = 9090
      desired_count = 1
    }
    alertmanager = {
      image         = "prom/alertmanager:v0.27.0"
      cpu           = 128
      memory        = 256
      port          = 9093
      desired_count = 1
    }
    pushgateway = {
      image         = "prom/pushgateway:v1.9.0"
      cpu           = 128
      memory        = 256
      port          = 9091
      desired_count = 1
    }
    airflow-webserver = {
      image             = "${local.ecr_urls["training"]}:${var.image_tag}" # docker/airflow image; reuses the training ECR repo slot, see README
      cpu               = 1024
      memory            = 2048
      port              = 8080
      public_subdomain  = "airflow"
      health_check_path = "/health"
      command           = ["webserver"]
      environment = {
        AIRFLOW__CORE__EXECUTOR                    = "LocalExecutor"
        AIRFLOW__DATABASE__SQL_ALCHEMY_CONN         = "postgresql+psycopg2://airflow:${module.secrets.postgres_password}@${module.rds_airflow.endpoint}/airflow"
        AIRFLOW__CORE__LOAD_EXAMPLES                = "false"
      }
      desired_count = 1
    }
    airflow-scheduler = {
      image     = "${local.ecr_urls["training"]}:${var.image_tag}"
      cpu       = 1024
      memory    = 2048
      command   = ["scheduler"]
      environment = {
        AIRFLOW__CORE__EXECUTOR            = "LocalExecutor"
        AIRFLOW__DATABASE__SQL_ALCHEMY_CONN = "postgresql+psycopg2://airflow:${module.secrets.postgres_password}@${module.rds_airflow.endpoint}/airflow"
        AIRFLOW__CORE__LOAD_EXAMPLES        = "false"
      }
      desired_count = 1
    }
  }
}

module "ecs" {
  source               = "../../modules/ecs"
  name_prefix          = local.name_prefix
  vpc_id               = module.vpc.vpc_id
  public_subnet_ids    = module.vpc.public_subnet_ids
  private_subnet_ids   = module.vpc.private_subnet_ids
  execution_role_arn   = module.iam.ecs_execution_role_arn
  task_role_arn        = module.iam.ecs_task_role_arn
  acm_certificate_arn  = var.acm_certificate_arn
  domain_name          = var.domain_name
  route53_zone_id      = var.route53_zone_id
  services             = local.services
  tags                 = local.tags
}

# ---------------------------------------------------------------------------
# Security group rules connecting ECS tasks to RDS/ElastiCache/MSK.
# Declared here (not inside modules/rds etc.) to avoid a module dependency
# cycle: rds/elasticache/msk need the ECS tasks' security group IDs, and
# the ecs module needs nothing back from them, so the edge is added
# after both sides exist.
# ---------------------------------------------------------------------------

resource "aws_security_group_rule" "ecs_to_rds_app" {
  for_each                 = module.ecs.task_security_group_ids
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  security_group_id        = module.rds_app.security_group_id
  source_security_group_id = each.value
}

resource "aws_security_group_rule" "ecs_to_rds_airflow" {
  for_each                 = { for k, v in module.ecs.task_security_group_ids : k => v if contains(["airflow-webserver", "airflow-scheduler"], k) }
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  security_group_id        = module.rds_airflow.security_group_id
  source_security_group_id = each.value
}

resource "aws_security_group_rule" "ecs_to_redis" {
  for_each                 = module.ecs.task_security_group_ids
  type                     = "ingress"
  from_port                = 6379
  to_port                  = 6379
  protocol                 = "tcp"
  security_group_id        = module.elasticache.security_group_id
  source_security_group_id = each.value
}

resource "aws_security_group_rule" "ecs_to_msk" {
  for_each                 = { for k, v in module.ecs.task_security_group_ids : k => v if contains(["producer", "consumer"], k) }
  type                     = "ingress"
  from_port                = 9094
  to_port                  = 9094
  protocol                 = "tcp"
  security_group_id        = module.msk.security_group_id
  source_security_group_id = each.value
}
