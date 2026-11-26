# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CognitoAuth.validate_token against real RS256 signatures.

python-jose was replaced by PyJWT (python-jose 3.5.0 has an open advisory and
no fixed release). These tests sign with a real RSA key and serve its public
half as the JWKS, so they pin the verification contract, not a mock: only the
configured key verifies, only RS256 is accepted, issuer and expiry are
enforced, and the unverified-claim readers never raise.
"""

import base64
import hashlib
import hmac
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import agents.auth.cognito as cognito_mod
from agents.auth.cognito import (
    CognitoAuth,
    TokenExpiredError,
    TokenValidationError,
)

REGION = "us-east-1"
POOL = "us-east-1_pool"
CLIENT = "client123"
ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{POOL}"


def _rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk(private_key, kid):
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    return {**jwk, "kid": kid, "alg": "RS256", "use": "sig"}


@pytest.fixture(scope="module")
def key():
    return _rsa_key()


@pytest.fixture(scope="module")
def other_key():
    return _rsa_key()


def _claims(**extra):
    now = int(time.time())
    return {
        "sub": "u-1",
        "iss": ISSUER,
        "token_use": "access",
        "client_id": CLIENT,
        "username": "alice",
        "email": "alice@example.com",
        "iat": now,
        "exp": now + 600,
        **extra,
    }


def _sign(private_key, claims, kid="kid-1", algorithm="RS256"):
    return jwt.encode(claims, private_key, algorithm=algorithm, headers={"kid": kid})


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _hs256(claims, secret: bytes, kid: str) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": kid}).encode())
    body = _b64(json.dumps(claims).encode())
    signing_input = f"{header}.{body}".encode()
    signature = hmac.new(secret, signing_input, hashlib.sha256).digest()
    return f"{header}.{body}.{_b64(signature)}"


def _auth(monkeypatch, *public_keys):
    auth = CognitoAuth(
        user_pool_id=POOL, client_id=CLIENT, client_secret=None, region=REGION
    )
    monkeypatch.setattr(CognitoAuth, "_get_jwks", lambda self: list(public_keys))
    return auth


def test_valid_token_returns_user_info(monkeypatch, key):
    auth = _auth(monkeypatch, _jwk(key, "kid-1"))
    token = _sign(key, _claims(**{"cognito:groups": ["Admin"]}))
    info = auth.validate_token(token)
    assert info.user_id == "u-1"
    assert info.email == "alice@example.com"
    assert info.groups == ["Admin"]


def test_key_is_selected_by_kid_among_several(monkeypatch, key, other_key):
    auth = _auth(monkeypatch, _jwk(other_key, "kid-0"), _jwk(key, "kid-1"))
    assert auth.validate_token(_sign(key, _claims())).user_id == "u-1"


def test_signature_from_a_different_key_is_rejected(monkeypatch, key, other_key):
    auth = _auth(monkeypatch, _jwk(key, "kid-1"))
    forged = _sign(other_key, _claims(), kid="kid-1")
    with pytest.raises(TokenValidationError):
        auth.validate_token(forged)


def test_unknown_kid_is_rejected(monkeypatch, key):
    auth = _auth(monkeypatch, _jwk(key, "kid-1"))
    with pytest.raises(TokenValidationError):
        auth.validate_token(_sign(key, _claims(), kid="kid-missing"))


def test_missing_kid_is_rejected(monkeypatch, key):
    auth = _auth(monkeypatch, _jwk(key, "kid-1"))
    token = jwt.encode(_claims(), key, algorithm="RS256")
    with pytest.raises(TokenValidationError):
        auth.validate_token(token)


def test_hs256_token_signed_with_the_public_key_is_rejected(monkeypatch, key):
    # Algorithm confusion: an attacker signs HS256 using the public JWK bytes.
    # PyJWT refuses to sign HS256 with a public key, so forge it by hand, with
    # the DER and PEM encodings the python-jose advisory names.
    auth = _auth(monkeypatch, _jwk(key, "kid-1"))
    public = key.public_key()
    for encoding in (serialization.Encoding.DER, serialization.Encoding.PEM):
        secret = public.public_bytes(
            encoding, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        forged = _hs256(_claims(), secret, kid="kid-1")
        with pytest.raises(TokenValidationError):
            auth.validate_token(forged)


def test_alg_none_token_is_rejected(monkeypatch, key):
    auth = _auth(monkeypatch, _jwk(key, "kid-1"))
    unsigned = jwt.encode(_claims(), None, algorithm="none", headers={"kid": "kid-1"})
    with pytest.raises(TokenValidationError):
        auth.validate_token(unsigned)


def test_expired_token_raises_token_expired(monkeypatch, key):
    auth = _auth(monkeypatch, _jwk(key, "kid-1"))
    now = int(time.time())
    token = _sign(key, _claims(iat=now - 7200, exp=now - 3600))
    with pytest.raises(TokenExpiredError):
        auth.validate_token(token)


def test_expired_token_is_accepted_when_expiry_check_is_off(monkeypatch, key):
    auth = _auth(monkeypatch, _jwk(key, "kid-1"))
    now = int(time.time())
    token = _sign(key, _claims(iat=now - 7200, exp=now - 3600))
    assert auth.validate_token(token, verify_exp=False).user_id == "u-1"


def test_wrong_issuer_is_rejected(monkeypatch, key):
    auth = _auth(monkeypatch, _jwk(key, "kid-1"))
    token = _sign(key, _claims(iss="https://example.com/other"))
    with pytest.raises(TokenValidationError):
        auth.validate_token(token)


def test_id_token_token_use_is_rejected(monkeypatch, key):
    auth = _auth(monkeypatch, _jwk(key, "kid-1"))
    with pytest.raises(TokenValidationError):
        auth.validate_token(_sign(key, _claims(token_use="id")))


def test_client_id_mismatch_is_rejected(monkeypatch, key):
    auth = _auth(monkeypatch, _jwk(key, "kid-1"))
    with pytest.raises(TokenValidationError):
        auth.validate_token(_sign(key, _claims(client_id="someone-else")))


def test_garbage_token_is_rejected(monkeypatch, key):
    auth = _auth(monkeypatch, _jwk(key, "kid-1"))
    with pytest.raises(TokenValidationError):
        auth.validate_token("not-a-jwt")


def test_token_with_clock_skewed_iat_is_still_accepted(monkeypatch, key):
    # Cognito and this host can disagree by seconds; iat is not a validity check.
    auth = _auth(monkeypatch, _jwk(key, "kid-1"))
    now = int(time.time())
    assert auth.validate_token(_sign(key, _claims(iat=now + 30))).user_id == "u-1"


def test_validated_user_carries_the_verified_exp_and_iat(monkeypatch, key):
    """The refresh timing reads exp/iat from the claims validate_token has
    verified, so no second, unverified decode of the token exists."""
    auth = _auth(monkeypatch, _jwk(key, "kid-1"))
    now = int(time.time())
    user = auth.validate_token(_sign(key, _claims(iat=now - 10, exp=now + 100)))
    assert user.expires_at == now + 100
    assert user.issued_at == now - 10


def test_validated_user_without_iat_has_no_issued_at(monkeypatch, key):
    auth = _auth(monkeypatch, _jwk(key, "kid-1"))
    claims = _claims()
    claims.pop("iat", None)
    user = auth.validate_token(_sign(key, claims))
    assert user.issued_at is None
    assert user.expires_at == claims["exp"]


def test_module_has_no_unverified_decode():
    source = open(cognito_mod.__file__).read()
    assert '"verify_signature": False' not in source
    assert not hasattr(cognito_mod, "token_exp") and not hasattr(cognito_mod, "token_iat")


def test_module_does_not_import_python_jose():
    assert not hasattr(cognito_mod, "JWTError")
    source = open(cognito_mod.__file__).read()
    assert "from jose" not in source and "import jose" not in source
