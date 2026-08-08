"""
AWS Secrets Manager integration, with local env-var configuration as the
default (unchanged from Phase 1/2 behavior).

Local dev / docker-compose: `USE_AWS_SECRETS_MANAGER=false` (the default)
-- credentials come from `.env` / docker-compose `environment:` blocks,
read directly by `Settings` via pydantic-settings, same as before this
module existed.

AWS deployment: most application secrets are *already* injected as ECS
container `secrets` (each task definition's `secretsmanager:GetSecretValue`
`valueFrom` reference -- see `terraform/environments/prod/main.tf`'s
`common_secrets` local), so those env vars are populated before the
process even starts and this module is redundant for them. This module
exists for the paths that injection doesn't cover: a local machine or
one-off script pointed at a deployed environment, an Airflow task that
isn't part of the ECS `services` map, or as a defense-in-depth fallback if
an expected env var is ever unset. Set `USE_AWS_SECRETS_MANAGER=true` and
`AWS_SECRETS_MANAGER_SECRET_ID` to opt in.

The blob shape this reads matches
`terraform/modules/secrets/main.tf`'s `aws_secretsmanager_secret_version.app`
`secret_string` exactly: one JSON object under one secret ID, not one
secret per credential (fewer `GetSecretValue` calls at cold start).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict

from src.common.config import Settings

logger = logging.getLogger(__name__)

# Maps a key in the Secrets Manager JSON blob to the Settings field it
# overrides. Only fields this application process itself reads at runtime
# are listed here -- `airflow_webserver_secret` and `grafana_admin_password`
# are also in the blob but are consumed directly by their own containers
# via ECS secrets injection, not by this Settings object.
_SETTINGS_FIELD_BY_SECRET_KEY = {
    "postgres_password": "postgres_password",
    "redis_auth_token": "redis_auth_token",
    "jwt_secret_key": "jwt_secret_key",
}


def fetch_secret_blob(settings: Settings) -> Dict[str, Any]:
    """Fetch and JSON-decode the platform's single Secrets Manager secret.

    Raises on any failure. If `USE_AWS_SECRETS_MANAGER=true`, a
    missing/unreadable secret means the process cannot be configured
    correctly -- failing loudly at startup is preferable to silently
    falling back to insecure defaults (e.g. the placeholder
    `change_me_in_prod` password).
    """
    import boto3  # local import: boto3 is only a hard dependency on this code path
    from botocore.exceptions import BotoCoreError, ClientError

    client = boto3.client("secretsmanager", region_name=settings.aws_region)
    try:
        response = client.get_secret_value(SecretId=settings.aws_secrets_manager_secret_id)
    except (BotoCoreError, ClientError):
        logger.exception(
            "Failed to fetch secret '%s' from AWS Secrets Manager in region '%s'. "
            "Set USE_AWS_SECRETS_MANAGER=false to fall back to local env vars instead.",
            settings.aws_secrets_manager_secret_id,
            settings.aws_region,
        )
        raise

    try:
        return json.loads(response["SecretString"])
    except (KeyError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"Secret '{settings.aws_secrets_manager_secret_id}' did not contain a "
            "valid JSON SecretString"
        ) from exc


def apply_aws_secrets(settings: Settings) -> Settings:
    """Return a new `Settings` with credential fields overridden from AWS
    Secrets Manager. Non-secret fields (hosts, ports, thresholds, feature
    flags, ...) are left untouched -- only the fields listed in
    `_SETTINGS_FIELD_BY_SECRET_KEY` are replaced.
    """
    blob = fetch_secret_blob(settings)

    missing = [key for key in _SETTINGS_FIELD_BY_SECRET_KEY if key not in blob]
    if missing:
        logger.warning(
            "AWS Secrets Manager blob '%s' is missing expected key(s) %s; "
            "those settings will keep their env-var/default values.",
            settings.aws_secrets_manager_secret_id,
            sorted(missing),
        )

    overrides = {
        settings_field: blob[secret_key]
        for secret_key, settings_field in _SETTINGS_FIELD_BY_SECRET_KEY.items()
        if secret_key in blob
    }
    return settings.model_copy(update=overrides)
