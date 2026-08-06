output "endpoint" {
  value = aws_db_instance.postgres.address
}

output "port" {
  value = aws_db_instance.postgres.port
}

output "security_group_id" {
  value = aws_security_group.rds.id
}

output "db_name" {
  value = aws_db_instance.postgres.db_name
}
