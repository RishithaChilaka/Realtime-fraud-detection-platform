# ECS Fargate cluster + one ALB fronting every service that declares a
# `public_subdomain` (host-based routing: api.<domain>, review.<domain>,
# grafana.<domain>, mlflow.<domain>, airflow.<domain>). Services without a
# `public_subdomain` (producer, consumer, airflow-scheduler) run as
# internal-only Fargate tasks with no ALB target group -- reachable from
# other services via AWS Cloud Map service discovery, not the internet.
#
# NOTE on the `consumer` service specifically: this deploys Spark
# Structured Streaming as a single Fargate task in `local[*]` mode
# (matching `SPARK_MASTER_URL=local[*]`), not a real multi-node Spark
# cluster -- Fargate's task model doesn't map cleanly onto Spark's
# master/worker cluster-manager assumptions the way docker-compose's
# spark-master/spark-worker containers do locally. A real production
# deployment of this pipeline would more likely run the streaming job on
# Amazon EMR (EMR on EKS or EMR Serverless) instead, using ECS/Fargate
# only for the stateless services (api, review-ui, producer, mlflow,
# airflow). That's a deliberate scope simplification, called out in the
# README, not an oversight.

resource "aws_ecs_cluster" "this" {
  name = "${var.name_prefix}-cluster"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = var.tags
}

resource "aws_ecs_cluster_capacity_providers" "this" {
  cluster_name       = aws_ecs_cluster.this.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
  }
}

resource "aws_service_discovery_private_dns_namespace" "this" {
  name = "${var.name_prefix}.internal"
  vpc  = var.vpc_id
}

# --- ALB (public services only) -----------------------------------------

resource "aws_security_group" "alb" {
  name_prefix = "${var.name_prefix}-alb-"
  vpc_id      = var.vpc_id
  description = "Internet-facing ALB: allow 80/443 from anywhere"

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-alb-sg" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_lb" "this" {
  name               = "${var.name_prefix}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.public_subnet_ids

  drop_invalid_header_fields = true

  tags = var.tags
}

resource "aws_lb_listener" "http_redirect" {
  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.acm_certificate_arn

  default_action {
    type = "fixed-response"
    fixed_response {
      content_type = "text/plain"
      message_body = "No service registered for this host"
      status_code  = "404"
    }
  }
}

locals {
  public_services = { for k, v in var.services : k => v if v.public_subdomain != null }
}

resource "aws_lb_target_group" "public" {
  for_each    = local.public_services
  name        = "${var.name_prefix}-${each.key}"
  port        = each.value.port
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  health_check {
    path                = each.value.health_check_path
    healthy_threshold   = 2
    unhealthy_threshold = 3
    interval            = 30
    timeout             = 5
    matcher             = "200-399"
  }

  tags = var.tags
}

resource "aws_lb_listener_rule" "public" {
  for_each     = local.public_services
  listener_arn = aws_lb_listener.https.arn
  priority     = 100 + index(keys(local.public_services), each.key)

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.public[each.key].arn
  }

  condition {
    host_header {
      values = ["${each.value.public_subdomain}.${var.domain_name}"]
    }
  }
}

resource "aws_route53_record" "public" {
  for_each = var.route53_zone_id != null ? local.public_services : {}
  zone_id  = var.route53_zone_id
  name     = "${each.value.public_subdomain}.${var.domain_name}"
  type     = "A"

  alias {
    name                   = aws_lb.this.dns_name
    zone_id                = aws_lb.this.zone_id
    evaluate_target_health = true
  }
}

# --- ECS tasks/services ---------------------------------------------------

resource "aws_security_group" "task" {
  for_each    = var.services
  name_prefix = "${var.name_prefix}-${each.key}-"
  vpc_id      = var.vpc_id
  description = "${each.key} Fargate task"

  dynamic "ingress" {
    for_each = each.value.port != null ? [1] : []
    content {
      from_port       = each.value.port
      to_port         = each.value.port
      protocol        = "tcp"
      security_groups = each.value.public_subdomain != null ? [aws_security_group.alb.id] : []
      self            = each.value.public_subdomain == null # internal services talk to each other on their own port
    }
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-${each.key}-sg" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_cloudwatch_log_group" "this" {
  for_each          = var.services
  name              = "/ecs/${var.name_prefix}/${each.key}"
  retention_in_days = 30
  tags              = var.tags
}

resource "aws_ecs_task_definition" "this" {
  for_each                 = var.services
  family                   = "${var.name_prefix}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = each.value.cpu
  memory                   = each.value.memory
  execution_role_arn       = var.execution_role_arn
  task_role_arn            = var.task_role_arn

  container_definitions = jsonencode([
    {
      name         = each.key
      image        = each.value.image
      essential    = true
      command      = each.value.command
      portMappings = each.value.port != null ? [{ containerPort = each.value.port, protocol = "tcp" }] : []
      environment  = [for k, v in each.value.environment : { name = k, value = v }]
      secrets      = [for k, arn in each.value.secrets : { name = k, valueFrom = arn }]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.this[each.key].name
          "awslogs-region"        = data.aws_region.current.name
          "awslogs-stream-prefix" = each.key
        }
      }
    }
  ])

  tags = var.tags
}

resource "aws_ecs_service" "this" {
  for_each        = var.services
  name            = "${var.name_prefix}-${each.key}"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.this[each.key].arn
  desired_count   = each.value.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.task[each.key].id]
    assign_public_ip = false
  }

  dynamic "load_balancer" {
    for_each = each.value.public_subdomain != null ? [1] : []
    content {
      target_group_arn = aws_lb_target_group.public[each.key].arn
      container_name   = each.key
      container_port   = each.value.port
    }
  }

  service_registries {
    registry_arn = aws_service_discovery_service.this[each.key].arn
  }

  deployment_maximum_percent         = 200
  deployment_minimum_healthy_percent = 100

  # Terraform provisions the *first* task definition revision; after that,
  # .github/workflows/deploy.yml registers new revisions (new image tag)
  # and calls `aws ecs update-service` directly on every merge to main.
  # Without this, the next `terraform apply` would silently roll the
  # service back to whatever image this config says -- fighting CI/CD.
  lifecycle {
    ignore_changes = [task_definition]
  }

  tags = var.tags
}

resource "aws_service_discovery_service" "this" {
  for_each = var.services
  name     = each.key

  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.this.id
    dns_records {
      ttl  = 10
      type = "A"
    }
  }

  health_check_custom_config {
    failure_threshold = 1
  }
}

data "aws_region" "current" {}
