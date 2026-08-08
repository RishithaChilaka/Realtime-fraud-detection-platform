"""POST /auth/token -- exchange demo credentials for a role-scoped JWT.
See src/api/auth.py's module docstring for the (explicit, documented)
limitations of the demo user store this issues tokens against."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from src.api.auth import authenticate_demo_user, create_access_token
from src.common.config import Settings, get_settings

router = APIRouter(tags=["auth"])


class TokenRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    expires_at: int


@router.post("/auth/token", response_model=TokenResponse)
def issue_token(body: TokenRequest, settings: Settings = Depends(get_settings)) -> TokenResponse:
    role = authenticate_demo_user(body.username, body.password)
    token, expires_at = create_access_token(subject=body.username, role=role, settings=settings)
    return TokenResponse(access_token=token, role=role, expires_at=expires_at)
