import json
import time

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from jose import jwt as jose_jwt
from jose.utils import base64url_decode, base64url_encode, long_to_base64

from app.core import security
from app.deps import _bearer_token, get_current_user
from app.models import User

ISSUER = "https://issuer.test"
KID = "test-key"
KINDE_ID = "kp_testuser123"


class StubSettings:
    kinde_issuer_url = ISSUER
    kinde_audience = None


def keys_for(public_key, kid=KID):
    """Build a JWK dict from an RSA public key, matching Kinde's format."""
    numbers = public_key.public_numbers()
    return {
        "kty": "RSA",
        "kid": kid,
        "alg": "RS256",
        "e": long_to_base64(numbers.e),
        "n": long_to_base64(numbers.n),
    }


def mint(private_key, claims, kid=KID):
    """Sign a JWT with the given RS256 private key and header kid."""
    return jose_jwt.encode(
        claims, private_key, algorithm="RS256", headers={"kid": kid, "typ": "JWT"}
    )


def valid_claims(**overrides):
    """Return default valid JWT claims (sub/iss/exp), overridable per test."""
    claims = {"sub": KINDE_ID, "iss": ISSUER, "exp": int(time.time()) + 3600}
    claims.update(overrides)
    return claims


def tamper(token):
    """Rebuild a token with an altered `sub` claim but the original signature."""
    header, payload, signature = token.split(".")
    data = json.loads(base64url_decode(payload))
    data["sub"] = "kp_attacker"
    new_payload = base64url_encode(json.dumps(data).encode())
    return f"{header}.{new_payload}.{signature}"


def reset_cache():
    """Clear the module-level JWKS cache so the next verify refetches."""
    security._jwks_cache["keys"] = None
    security._jwks_cache["fetched_at"] = 0.0


@pytest.fixture()
def rsa_keypair():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture()
def auth_env(monkeypatch, rsa_keypair):
    private_key = rsa_keypair
    public_key = private_key.public_key()
    jwks = [keys_for(public_key)]
    monkeypatch.setattr(security, "get_settings", lambda: StubSettings())
    monkeypatch.setattr(security, "_fetch_jwks", _async_return(jwks))
    reset_cache()
    yield {"private_key": private_key, "jwks": jwks}
    reset_cache()


def _async_return(value):
    """Return an async callable that yields a fixed value."""

    async def _fake():
        return value

    return _fake


async def test_jwks_url_format(monkeypatch):
    monkeypatch.setattr(security, "get_settings", lambda: StubSettings())
    assert security._jwks_url() == f"{ISSUER}/.well-known/jwks.json"


async def test_valid_token_accepted(auth_env):
    token = mint(auth_env["private_key"], valid_claims())
    payload = await security.verify_token(token)
    assert payload["sub"] == KINDE_ID
    assert payload["iss"] == ISSUER


async def test_expired_token_rejected(auth_env):
    token = mint(auth_env["private_key"], valid_claims(exp=int(time.time()) - 100))
    with pytest.raises(HTTPException) as exc:
        await security.verify_token(token)
    assert exc.value.status_code == 401


async def test_tampered_payload_rejected(auth_env):
    token = mint(auth_env["private_key"], valid_claims())
    with pytest.raises(HTTPException) as exc:
        await security.verify_token(tamper(token))
    assert exc.value.status_code == 401


async def test_wrong_issuer_rejected(auth_env):
    token = mint(auth_env["private_key"], valid_claims(iss="https://evil.test"))
    with pytest.raises(HTTPException) as exc:
        await security.verify_token(token)
    assert exc.value.status_code == 401


async def test_wrong_audience_rejected(auth_env, monkeypatch):
    monkeypatch.setattr(
        security,
        "get_settings",
        lambda: type("S", (), {"kinde_issuer_url": ISSUER, "kinde_audience": "aud-1"})(),
    )
    token = mint(auth_env["private_key"], valid_claims(aud="aud-2"))
    with pytest.raises(HTTPException) as exc:
        await security.verify_token(token)
    assert exc.value.status_code == 401


async def test_matching_audience_accepted(auth_env, monkeypatch):
    monkeypatch.setattr(
        security,
        "get_settings",
        lambda: type("S", (), {"kinde_issuer_url": ISSUER, "kinde_audience": "aud-1"})(),
    )
    token = mint(auth_env["private_key"], valid_claims(aud="aud-1"))
    payload = await security.verify_token(token)
    assert payload["aud"] == "aud-1"


async def test_unknown_kid_rejected(auth_env):
    token = mint(
        auth_env["private_key"],
        valid_claims(),
        kid="other-kid",
    )
    with pytest.raises(HTTPException) as exc:
        await security.verify_token(token)
    assert exc.value.status_code == 401


async def test_malformed_token_rejected(auth_env):
    with pytest.raises(HTTPException) as exc:
        await security.verify_token("not-a-jwt")
    assert exc.value.status_code == 401


async def test_jwks_fetch_failure_is_503(monkeypatch, rsa_keypair):
    token = mint(rsa_keypair, valid_claims())

    async def _boom():
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(security, "get_settings", lambda: StubSettings())
    monkeypatch.setattr(security, "_fetch_jwks", _boom)
    reset_cache()
    with pytest.raises(HTTPException) as exc:
        await security.verify_token(token)
    assert exc.value.status_code == 503


async def test_jwks_empty_is_503(monkeypatch, rsa_keypair):
    token = mint(rsa_keypair, valid_claims())
    monkeypatch.setattr(security, "get_settings", lambda: StubSettings())
    monkeypatch.setattr(security, "_fetch_jwks", _async_return([]))
    reset_cache()
    with pytest.raises(HTTPException) as exc:
        await security.verify_token(token)
    assert exc.value.status_code == 503


async def test_jwks_cached_between_verifies(auth_env, monkeypatch):
    calls = {"n": 0}

    async def _counting():
        calls["n"] += 1
        return auth_env["jwks"]

    monkeypatch.setattr(security, "_fetch_jwks", _counting)
    reset_cache()
    token = mint(auth_env["private_key"], valid_claims())
    await security.verify_token(token)
    await security.verify_token(token)
    assert calls["n"] == 1


class _FakeResult:
    """Stub query result exposing scalar_one_or_none()."""

    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _FakeSession:
    """Stub async session returning a fixed user from any execute()."""

    def __init__(self, user=None):
        self.user = user

    async def execute(self, statement):
        return _FakeResult(self.user)


class _FakeState:
    """Minimal request.state stand-in for caching assertions."""

    def __init__(self):
        self.current_user = None


class _FakeRequest:
    """Minimal Request stand-in carrying headers and a mutable state."""

    def __init__(self, auth=None):
        self.headers = {"Authorization": auth} if auth else {}
        self.state = _FakeState()


def _stub_user():
    """Construct a detached User instance without touching the database."""
    return User(id="u-1", kinde_id=KINDE_ID, email="user@example.com")


def test_bearer_token_missing():
    with pytest.raises(HTTPException) as exc:
        _bearer_token(_FakeRequest(auth=None))
    assert exc.value.status_code == 401


def test_bearer_token_malformed():
    with pytest.raises(HTTPException) as exc:
        _bearer_token(_FakeRequest(auth="Basic abc123"))
    assert exc.value.status_code == 401


async def test_current_user_not_authenticated():
    with pytest.raises(HTTPException) as exc:
        await get_current_user(_FakeRequest(auth=None), _FakeSession())
    assert exc.value.status_code == 401


async def test_current_user_registration_incomplete(auth_env):
    token = mint(auth_env["private_key"], valid_claims())
    request = _FakeRequest(auth=f"Bearer {token}")
    with pytest.raises(HTTPException) as exc:
        await get_current_user(request, _FakeSession(user=None))
    assert exc.value.status_code == 401
    assert "Registration" in exc.value.detail


async def test_current_user_ok(auth_env):
    token = mint(auth_env["private_key"], valid_claims())
    request = _FakeRequest(auth=f"Bearer {token}")
    user = await get_current_user(request, _FakeSession(user=_stub_user()))
    assert user.kinde_id == KINDE_ID
    assert request.state.current_user is user


async def test_current_user_uses_per_request_cache(auth_env):
    token = mint(auth_env["private_key"], valid_claims())
    request = _FakeRequest(auth=f"Bearer {token}")
    first = await get_current_user(request, _FakeSession(user=_stub_user()))
    second = await get_current_user(request, _FakeSession(user=None))
    assert first is second