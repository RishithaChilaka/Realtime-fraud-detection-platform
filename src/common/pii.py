"""
PII masking utilities for logs and audit trails.

Two masking strategies, used for different purposes:
  - `mask_identifier`: keeps a short prefix and replaces the remainder with
    a deterministic (salted) hash suffix. Two log lines referencing the
    same card_id still visibly correlate (useful for debugging "what else
    did this card do") without the raw identifier appearing in plaintext
    logs -- which matters because logs routinely have laxer access control
    and longer retention than the primary database.
  - `mask_ip_address`: zeroes the last octet/segment, which is enough to
    break precise re-identification while keeping the network/region
    visible for abuse investigation.

This module does NOT touch what's stored in PostgreSQL -- `transactions`,
`predictions`, etc. intentionally keep raw values, because the audit trail
requirement (Phase 2) needs the real data for compliance review. Masking
applies specifically to logs (`src/common/logging_config.py`'s structlog
processor) and to any free-text fields callers choose to build with
`redact_dict`, e.g. before writing an `audit_logs.message` string that
might otherwise echo a raw identifier into a less-tightly-controlled log
aggregation system.
"""
from __future__ import annotations

import hashlib
import os
from typing import Any

# Fields masked automatically by the structlog processor
# (`redact_pii_processor`) on every log line, regardless of which module
# emitted it.
PII_FIELD_NAMES = frozenset(
    {
        "card_id",
        "ip_address",
        "client_ip",
        "device_id",
        "user_id",
        "email",
        "analyst_id",
    }
)

_SALT = os.environ.get("PII_MASK_SALT", "fraud-detection-platform-default-salt")


def _hash_suffix(value: str, length: int = 6) -> str:
    digest = hashlib.sha256((_SALT + value).encode("utf-8")).hexdigest()
    return digest[:length]


def mask_identifier(value: str | None, keep_prefix: int = 6) -> str | None:
    """`card_a1b2c3d4e5f6` -> `card_a1***7f2a1c`. Deterministic: the same
    input always produces the same masked output, so it's safe to grep
    logs for a specific masked value while never storing/transmitting the
    raw one in that context."""
    if not value:
        return value
    prefix = value[:keep_prefix]
    return f"{prefix}***{_hash_suffix(value)}"


def mask_ip_address(ip: str | None) -> str | None:
    if not ip:
        return ip
    if ":" in ip:  # IPv6, mask the last two groups
        parts = ip.split(":")
        if len(parts) >= 2:
            parts[-1] = "xxxx"
            parts[-2] = "xxxx"
        return ":".join(parts)
    parts = ip.split(".")
    if len(parts) == 4:
        parts[-1] = "xxx"
        return ".".join(parts)
    return mask_identifier(ip, keep_prefix=4)


def redact_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of `data` with any key in `PII_FIELD_NAMES` masked.
    Non-PII keys and nested non-dict values pass through unchanged; used
    ad hoc wherever a caller is about to log or otherwise externalize a
    dict that might contain raw identifiers (e.g. building an audit log
    message from a transaction payload)."""
    redacted = {}
    for key, value in data.items():
        if key in PII_FIELD_NAMES and isinstance(value, str):
            redacted[key] = mask_ip_address(value) if "ip" in key else mask_identifier(value)
        else:
            redacted[key] = value
    return redacted


def redact_pii_processor(logger, method_name, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog processor: masks any top-level key in `PII_FIELD_NAMES`.
    Registered in `src/common/logging_config.py`'s processor chain so
    every structured log call across every service gets this for free --
    no per-callsite opt-in required."""
    for key in list(event_dict.keys()):
        if key in PII_FIELD_NAMES and isinstance(event_dict[key], str):
            value = event_dict[key]
            event_dict[key] = mask_ip_address(value) if "ip" in key else mask_identifier(value)
    return event_dict
