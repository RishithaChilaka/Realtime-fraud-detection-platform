"""Structured logging setup shared by every service (producer, consumer, monitoring)."""
import logging
import sys

import structlog

from src.common.pii import redact_pii_processor


def configure_logging(service_name: str, level: int = logging.INFO) -> structlog.BoundLogger:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            # PII masking runs on every log line, for every service, before
            # rendering -- see src/common/pii.py for what gets masked and why.
            redact_pii_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    return structlog.get_logger(service_name)
