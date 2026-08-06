# PostgreSQL for transactions/predictions/audit tables. Encrypted at rest
# (KMS) and forces TLS in transit via a custom parameter group
# (rds.force_ssl=1) -- see the security-hardening note in the README for
# why both matter for this workload (payment-adjacent data + audit trail).

resource "aws_db_subnet_group" "this" {
  name       = "${var.name_prefix}-postgres"
  subnet_ids = var.private_subnet_ids
  tags       = var.tags
}

resource "aws_security_group" "rds" {
  name_prefix = "${var.name_prefix}-rds-"
  vpc_id      = var.vpc_id
  description = "Postgres (5432) -- ingress rules added separately via aws_security_group_rule in the calling environment (see environments/prod/main.tf), not inline here, to avoid the well-known Terraform AWS provider conflict between inline ingress blocks and standalone aws_security_group_rule resources on the same SG"

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-rds-sg" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_db_parameter_group" "this" {
  name_prefix = "${var.name_prefix}-postgres-"
  family      = "postgres16"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  tags = var.tags
}

resource "aws_kms_key" "rds" {
  description             = "${var.name_prefix} RDS encryption at rest"
  deletion_window_in_days = 7
  enable_key_rotation     = true
  tags                    = var.tags
}

resource "aws_db_instance" "postgres" {
  identifier     = "${var.name_prefix}-postgres"
  engine         = "postgres"
  engine_version = "16.3"
  instance_class = var.instance_class

  allocated_storage     = var.allocated_storage_gb
  max_allocated_storage = var.max_allocated_storage_gb
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = aws_kms_key.rds.arn

  db_name  = var.db_name
  username = var.db_username
  password = var.db_password # sourced from Secrets Manager (see modules/secrets); never a literal in tfvars

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  parameter_group_name   = aws_db_parameter_group.this.name

  multi_az                     = var.multi_az
  backup_retention_period      = var.backup_retention_days
  deletion_protection          = var.deletion_protection
  skip_final_snapshot          = !var.deletion_protection
  final_snapshot_identifier    = var.deletion_protection ? "${var.name_prefix}-postgres-final" : null
  performance_insights_enabled = true
  publicly_accessible          = false

  tags = var.tags
}
