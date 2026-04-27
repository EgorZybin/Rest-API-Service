from __future__ import annotations

from typing import Annotated

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

from sherlock_api.config import get_settings

API_KEY_HEADER_NAME = "X-API-Key"
_api_key_scheme = APIKeyHeader(name=API_KEY_HEADER_NAME, auto_error=False, description="API key")


async def require_api_key(
    api_key: Annotated[str | None, Security(_api_key_scheme)],
) -> None:
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing API key",
            headers={"WWW-Authenticate": API_KEY_HEADER_NAME},
        )

    expected = get_settings().normalized_client_api_key
    if not expected or api_key != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key")
