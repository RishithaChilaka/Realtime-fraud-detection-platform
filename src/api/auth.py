"""
JWT authentication + role-based access control for the FastAPI service.

Roles:
  - `service`  -- machine-to-machine callers (the Spark consumer / other
                  internal services). Currently unused for `/score` and
                  `/explain`, which are intentionally left unauthenticated
                  (see note below), but defined for when that changes.
  - `analyst`  -- can read/act on the review queue (`/review/*`).
  - `admin`    -- analyst permissions plus operational endpoints
                  (`/admin/reload-model`).

Why `/score`/`/explain` are unauthenticated by default: they're called at
high volume, low latency, from trusted internal services (the streaming
pipeline, the Streamlit UI's `/explain` re-check), not directly by end
users. In a real AWS deployment (see `terraform/`), the boundary of trust
is the VPC/security-group perimeter and mTLS between the API and its
callers via the service mesh/ALB, not a JWT on every call -- adding JWT
there too is a straightforward extension (`Depends(require_role("service"))`
on those two routers) if the threat model calls for it.

Demo user store: this module ships a **hardcoded, in-memory** user list
purely so the RBAC mechanism is runnable end to end without standing up a
real identity provider. It is explicitly not production-ready -- see the
big warning below and the README's security section, which points at AWS
Cognito / an OIDC provider as the real replacement.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.common.config import Settings, get_settings

Role = Literal["service", "analyst", "admin"]

_security = HTTPBearer(auto_error=False)

# --- DEMO USER STORE -- replace with a real IdP before shipping this. -----
# In-memory, plaintext-password demo credentials so `/auth/token` is
# runnable locally without external dependencies. A production deployment
# should swap this for AWS Cognito (or any OIDC provider) issuing/verifying
# the JWTs instead, and delete this dict entirely.
_DEMO_USERS: dict[str, dict[str, str]] = {
    "analyst1": {"password": "analyst-demo-pass", "role": "analyst"},
    "admin1": {"password": "admin-demo-pass", "role": "admin"},
    "service-account": {"password": "service-demo-pass", "role": "service"},
}
# ---------------------------------------------------------------------------


@dataclass
class TokenPayload:
    subject: str
    role: Role
    expires_at: int


def authenticate_demo_user(username: str, password: str) -> Role:
    user = _DEMO_USERS.get(username)
    if user is None or user["password"] != password:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    return user["role"]  # type: ignore[return-value]


def create_access_token(subject: str, role: Role, settings: Settings) -> tuple[str, int]:
    expires_at = int(time.time()) + settings.jwt_expiry_minutes * 60
    payload = {"sub": subject, "role": role, "exp": expires_at}
    token = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return token, expires_at


def decode_token(token: str, settings: Settings) -> TokenPayload:
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    return TokenPayload(subject=payload["sub"], role=payload["role"], expires_at=payload["exp"])


def get_current_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_security),
    settings: Settings = Depends(get_settings),
) -> TokenPayload:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return decode_token(credentials.credentials, settings)


def require_role(*allowed_roles: Role):
    """FastAPI dependency factory: `Depends(require_role("analyst", "admin"))`
    on a route rejects with 403 unless the caller's JWT role is in
    `allowed_roles`. Kept as a factory (not a single fixed dependency) so
    each route declares exactly which roles it accepts, visible in the
    route definition itself."""

    def _check(token: TokenPayload = Depends(get_current_token)) -> TokenPayload:
        if token.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{token.role}' is not permitted; requires one of {allowed_roles}",
            )
        return token

    return _check
