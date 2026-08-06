# ElastiCache Redis backing the low-latency feature store
# (src/feature_engineering/feature_store.py). Encrypted at rest and in
# transit, with an AUTH token -- unlike the local docker-compose Redis
# (no auth, plaintext), this is the production posture the src/storage
# RedisClient's TLS/AUTH support (see redis_client.py) is meant for.

resource "aws_elasticache_subnet_group" "this" {
  name       = "${var.name_prefix}-redis"
  subnet_ids = var.private_subnet_ids
}

resource "aws_security_group" "redis" {
  name_prefix = "${var.name_prefix}-redis-"
  vpc_id      = var.vpc_id
  description = "Redis (6379, TLS) -- ingress added separately via aws_security_group_rule in the calling environment; see modules/rds/main.tf's comment for why inline ingress isn't used here"

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-redis-sg" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_elasticache_replication_group" "this" {
  replication_group_id       = "${var.name_prefix}-redis"
  description                = "Fraud detection feature store"
  engine                     = "redis"
  engine_version             = "7.1"
  node_type                  = var.node_type
  num_cache_clusters         = var.num_cache_clusters
  automatic_failover_enabled = var.num_cache_clusters > 1
  port                       = 6379

  subnet_group_name  = aws_elasticache_subnet_group.this.name
  security_group_ids = [aws_security_group.redis.id]

  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  transit_encryption_mode    = "required"
  auth_token                 = var.auth_token # sourced from Secrets Manager

  snapshot_retention_limit = 5

  tags = var.tags
}
