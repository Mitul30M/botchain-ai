import time

import httpx
from fastapi import HTTPException, status
from jose import jwt

from app.config import get_settings

_JWKS_URL_SUFFIX = "/.well-known/jwks.json"
_JWKS_TTL_SECONDS = 600.0

_jwks_cache: dict = {"fetched_at": 0.0, "keys": None}


def _jwks_url() -> str:
    """Build the kinde-serving JWKS endpoint URL from the configured issuer."""
    issuer = get_settings().kinde_issuer_url.rstrip("/")
    return f"{issuer}{_JWKS_URL_SUFFIX}"


def _unauthorized() -> HTTPException:
    """Return a 401 exception with the standard Bearer challenge header."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _fetch_jwks() -> list[dict]:
    """Fetch the raw JWKS key list from the Kinde issuer."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(_jwks_url())
    response.raise_for_status()
    return response.json().get("keys", [])


async def _get_jwks() -> list[dict]:
    """Return cached JWKS keys, refreshing from Kinde after the TTL elapses.

    Raises 503 (service unavailable) if the keys cannot be fetched.
    """
    now = time.monotonic()
    keys = _jwks_cache.get("keys")
    fetched_at = _jwks_cache.get("fetched_at", 0.0)
    if keys is None or now - fetched_at > _JWKS_TTL_SECONDS:
        try:
            keys = await _fetch_jwks()
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Unable to fetch signing keys",
            ) from exc
        if not keys:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="No signing keys available",
            )
        _jwks_cache["keys"] = keys
        _jwks_cache["fetched_at"] = now
    return keys


async def verify_token(token: str) -> dict:
    """Verify a Kinde bearer JWT (signature, issuer, audience, expiry).

    Returns the decoded claims on success; raises 401 on any invalid/expired
    token and 503 if the signing keys cannot be fetched.
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.JWTError:
        raise _unauthorized() from None

    key = next((k for k in await _get_jwks() if k.get("kid") == header.get("kid")), None)
    if key is None:
        raise _unauthorized()

    settings = get_settings()
    options = {
        "verify_iss": True,
        "verify_exp": True,
        "verify_aud": bool(settings.kinde_audience),
    }
    kwargs = {"audience": settings.kinde_audience} if settings.kinde_audience else {}
    try:
        return jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            issuer=settings.kinde_issuer_url,
            options=options,
            **kwargs,
        )
    except jwt.JWTError:
        raise _unauthorized() from None