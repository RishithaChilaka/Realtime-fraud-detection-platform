# Terraform: AWS deployment

```
terraform/
  modules/
    vpc/           2 AZs, public + private subnets, NAT gateway(s)
    ecr/           one repo per service image (producer/consumer/api/training/streamlit)
    rds/           PostgreSQL, encrypted at rest (KMS) + TLS enforced (rds.force_ssl); used twice (app db, airflow metadata db)
    elasticache/   Redis, encrypted at rest + in transit, AUTH token
    msk/           managed Kafka, TLS in transit + KMS at rest
    secrets/       AWS Secrets Manager: DB password, Redis auth token, JWT secret, Grafana admin password
    iam/           ECS execution/task roles + GitHub Actions OIDC deploy role (no static AWS keys in CI)
    ecs/           Fargate cluster, ALB (host-based routing, ACM/TLS), one task def + service per app service
  environments/
    prod/          wires every module together; the only environment provided (copy it for staging/dev)
```

## Before you run this

This was written and reviewed in a sandboxed environment with **no AWS
account, no Terraform binary, and no network access** to run
`terraform validate`/`plan`/`apply` against. Every file was hand-reviewed
for HCL syntax and AWS provider resource-argument correctness, and a
brace/paren-balance check was run across all `.tf` files, but **this has
not been applied to a real AWS account.** Treat it as a strong, realistic
starting point that still needs a real `terraform plan` review before
`apply`, not as pre-verified infrastructure.

## One-time bootstrap (do this manually, once, per AWS account)

Terraform state needs somewhere to live before `terraform init` can use
the S3 backend referenced (commented out) in `environments/prod/versions.tf`:

```bash
aws s3api create-bucket --bucket fraud-detection-platform-tfstate --region us-east-1
aws s3api put-bucket-versioning --bucket fraud-detection-platform-tfstate \
    --versioning-configuration Status=Enabled
aws dynamodb create-table --table-name fraud-detection-platform-tflock \
    --attribute-definitions AttributeName=LockID,AttributeType=S \
    --key-schema AttributeName=LockID,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST
```

Then uncomment the `backend "s3" {...}` block in `versions.tf`.

You also need, before `terraform apply`:
- A Route53-registered (or delegated) domain.
- An ACM certificate covering `*.<domain_name>`, issued in the same region
  as `aws_region` (ALB certificates are regional).

## Running it

```bash
cd terraform/environments/prod
cp terraform.tfvars.example terraform.tfvars
# edit terraform.tfvars: domain_name, acm_certificate_arn, route53_zone_id

terraform init
terraform plan   # review carefully before apply -- this provisions RDS, MSK,
                  # ElastiCache, and an ALB, all billable resources
terraform apply
```

After `apply`, push images to the ECR repos it created (`terraform output
ecr_repository_urls`) and the ECS services will start pulling on their
next deployment -- or just let `.github/workflows/deploy.yml` do that on
your next merge to `main` (see that workflow for the exact steps).

## Wiring up CI/CD (GitHub Actions -> ECR/ECS)

After the first `terraform apply`, set these as repo **variables** (not
secrets -- none of them are sensitive) under Settings -> Secrets and
variables -> Actions -> Variables, so `.github/workflows/deploy.yml` can
find your infrastructure:

| Variable            | Where to get it                                          |
|----------------------|-----------------------------------------------------------|
| `AWS_DEPLOY_ROLE_ARN` | `terraform output github_deploy_role_arn`                 |
| `AWS_ACCOUNT_ID`      | `aws sts get-caller-identity --query Account --output text` |
| `ECS_CLUSTER_NAME`    | `terraform output ecs_cluster_name`                        |
| `AWS_REGION`          | your `aws_region` var (default `us-east-1`)                |
| `NAME_PREFIX`         | your `name_prefix` var (default `fraud-detection`)         |

No `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` are needed anywhere --
`AWS_DEPLOY_ROLE_ARN` is assumed via the GitHub OIDC federation set up in
`modules/iam` (see that module's `github_deploy` resources), scoped to
this one repo.

Once those are set, every push to `main` that passes `ci.yml` triggers
`deploy.yml`: it builds+pushes the five service images to ECR tagged with
the commit SHA, then for each ECS service registers a new task definition
revision pointing at that image and calls
`aws-actions/amazon-ecs-deploy-task-definition` (which does the
`update-service` + wait-for-stability). Terraform never fights this --
the `ecs` module's `lifecycle { ignore_changes = [task_definition] }` means
`terraform apply` won't roll a service back to the image tag baked into
`environments/prod`'s `image_tag` var.

## Deliberate scope simplifications (and why)

- **`consumer` runs Spark in `local[*]` mode on a single Fargate task**,
  not a clustered Spark deployment. Fargate's task model doesn't map onto
  Spark's master/worker cluster-manager assumptions the way
  docker-compose's `spark-master`/`spark-worker` containers do locally. A
  real production deployment of this pipeline would more likely run the
  streaming job on Amazon EMR (EMR on EKS or EMR Serverless) instead of
  raw ECS/Fargate, and use ECS/Fargate only for the stateless services.
- **ECS Fargate, not EKS**, per the deliverable's "EKS/ECS" either-or.
  Fargate was chosen for less operational boilerplate (no node groups,
  no IRSA setup, no cluster autoscaler) at the cost of some flexibility
  EKS would give you (DaemonSets, more exotic scheduling). Swapping to
  EKS would replace the `ecs` module; nothing else in `terraform/`
  depends on the choice.
- **Airflow's metadata DB reuses the same generated password as the app
  DB** (`module.rds_airflow.db_password = module.secrets.postgres_password`)
  for brevity. A stricter separation would add a second
  `random_password` resource in `modules/secrets` -- straightforward, just
  trimmed here to keep the example's secret surface smaller.
- **No WAF, no CloudFront, no cross-region DR.** Reasonable next additions
  for a real production posture, out of scope for this pass.
- **The demo JWT user store** (`src/api/auth.py`) is in-memory and
  plaintext -- fine for exercising the RBAC mechanism end to end, not a
  real identity provider. See that file's docstring for the AWS Cognito
  swap-in path.
