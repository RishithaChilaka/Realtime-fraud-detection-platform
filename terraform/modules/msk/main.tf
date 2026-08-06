# MSK (managed Kafka) replacing the docker-compose single-broker KRaft
# setup for the cloud deployment. TLS in transit + KMS encryption at rest;
# the producer/consumer's KAFKA_BOOTSTRAP_SERVERS points at the TLS
# listener (port 9094) in production, not the plaintext one.

resource "aws_kms_key" "msk" {
  description             = "${var.name_prefix} MSK encryption at rest"
  deletion_window_in_days = 7
  enable_key_rotation     = true
  tags                    = var.tags
}

resource "aws_security_group" "msk" {
  name_prefix = "${var.name_prefix}-msk-"
  vpc_id      = var.vpc_id
  description = "Kafka TLS (9094) + JMX (11001-11002) -- ingress added separately via aws_security_group_rule in the calling environment; see modules/rds/main.tf's comment for why inline ingress isn't used here"

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-msk-sg" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_msk_configuration" "this" {
  name           = "${var.name_prefix}-msk-config"
  kafka_versions = [var.kafka_version]

  server_properties = <<-PROPERTIES
    auto.create.topics.enable=true
    default.replication.factor=${var.broker_count >= 3 ? 3 : var.broker_count}
    min.insync.replicas=${var.broker_count >= 3 ? 2 : 1}
    num.partitions=6
  PROPERTIES
}

resource "aws_msk_cluster" "this" {
  cluster_name           = "${var.name_prefix}-kafka"
  kafka_version           = var.kafka_version
  number_of_broker_nodes  = var.broker_count

  broker_node_group_info {
    instance_type   = var.broker_instance_type
    client_subnets  = var.private_subnet_ids
    security_groups = [aws_security_group.msk.id]

    storage_info {
      ebs_storage_info {
        volume_size = var.broker_ebs_volume_gb
      }
    }
  }

  configuration_info {
    arn      = aws_msk_configuration.this.arn
    revision = aws_msk_configuration.this.latest_revision
  }

  encryption_info {
    encryption_at_rest_kms_key_arn = aws_kms_key.msk.arn
    encryption_in_transit {
      client_broker = "TLS"
      in_cluster    = true
    }
  }

  enhanced_monitoring = "PER_TOPIC_PER_BROKER"

  logging_info {
    broker_logs {
      cloudwatch_logs {
        enabled   = true
        log_group = var.cloudwatch_log_group_name
      }
    }
  }

  tags = var.tags
}
