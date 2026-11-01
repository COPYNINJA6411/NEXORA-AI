"""Environment-configured JWT authentication and role checks for the API."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pwdlib import PasswordHash


password_hasher = PasswordHash.recommended()
bearer_scheme = HTTPBearer(auto_error=False)
VALID_ROLES = {"viewer", "analyst", "admin"}


@dataclass(frozen=True)
class Principal:
    username: str
    role: str


def auth_enabled() -> bool:
    override = os.getenv("NEXORA_AUTH_ENABLED")
    return override.lower() == "true" if override is not None else bool(os.getenv("NEXORA_USERS_JSON", "").strip())


def _secret() -> str:
    secret = os.getenv("NEXORA_JWT_SECRET", "")
    if len(secret) < 32:
        raise HTTPException(status_code=503, detail="Configure a NEXORA_JWT_SECRET with at least 32 characters.")
    return secret


def users() -> dict[str, dict[str, str]]:
    raw = os.getenv("NEXORA_USERS_JSON", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=503, detail="NEXORA_USERS_JSON is not valid JSON.") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=503, detail="NEXORA_USERS_JSON must be a JSON object keyed by username.")
    for username, user in parsed.items():
        if not isinstance(username, str) or not isinstance(user, dict):
            raise HTTPException(status_code=503, detail="Invalid user configuration.")
        if user.get("role") not in VALID_ROLES or not isinstance(user.get("password_hash"), str):
            raise HTTPException(status_code=503, detail=f"Invalid role or password hash for user '{username}'.")
    return parsed


def issue_token(username: str, password: str) -> tuple[str, int, str]:
    configured = users()
    user = configured.get(username)
    if not user or not password_hasher.verify(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid username or password.", headers={"WWW-Authenticate": "Bearer"})
    lifetime_seconds = int(os.getenv("NEXORA_TOKEN_TTL_SECONDS", "3600"))
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {"sub": username, "role": user["role"], "iat": now, "exp": now + timedelta(seconds=lifetime_seconds)},
        _secret(),
        algorithm="HS256",
    )
    return token, lifetime_seconds, user["role"]


def current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> Principal:
    if not auth_enabled():
        return Principal(username="local-demo", role="admin")
    if credentials is None:
        raise HTTPException(status_code=401, detail="Bearer token required.", headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = jwt.decode(credentials.credentials, _secret(), algorithms=["HS256"])
        username = payload.get("sub")
        role = payload.get("role")
        if not isinstance(username, str) or role not in VALID_ROLES:
            raise ValueError("Invalid token claims")
        return Principal(username=username, role=role)
    except (jwt.PyJWTError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired bearer token.", headers={"WWW-Authenticate": "Bearer"}) from exc


def require_roles(*allowed_roles: str) -> Callable[..., Principal]:
    def dependency(principal: Principal = Depends(current_principal)) -> Principal:
        if principal.role not in allowed_roles:
            raise HTTPException(status_code=403, detail="Your role is not allowed to use this operation.")
        return principal
    return dependency
