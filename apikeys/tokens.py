"""Tokens Bearer propios firmados con HMAC-SHA256, con TTL, refresco acotado y rotación de llaves de firma.

Formato: v1.<kid>.<payload b64url>.<firma b64url>. Un anillo de llaves (SigningKeyRing) permite rotar la llave de
firma sin invalidar de golpe los tokens vigentes: la llave anterior sigue verificando durante el periodo de gracia.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from typing import Callable


class TokenInvalid(ValueError):
    """Token rechazado. `reason`: malformed, unknown_kid, bad_signature, expired, scope, max_lifetime."""

    def __init__(self, reason: str):
        super().__init__("token inválido")
        self.reason = reason


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class SigningKeyRing:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._keys: dict[str, tuple[bytes, float | None]] = {}  # kid -> (secreto, retire_at)
        self._current: str | None = None
        self._lock = threading.Lock()

    def add_key(self, kid: str, secret: bytes, *, retire_at: float | None = None, current: bool = True) -> None:
        if len(secret) < 32:
            raise ValueError("la llave de firma debe tener al menos 32 bytes")
        if not kid or "." in kid:
            raise ValueError("kid inválido")
        with self._lock:
            self._keys[kid] = (secret, retire_at)
            if current:
                self._current = kid

    def rotate(self, *, grace: float = 3600.0, secret: bytes | None = None) -> str:
        """Crea una llave nueva (aleatoria) como vigente; la anterior verifica `grace` segundos más."""
        with self._lock:
            now = self._clock()
            if self._current in self._keys:
                old_secret, _ = self._keys[self._current]
                self._keys[self._current] = (old_secret, now + max(0.0, grace))
            kid = secrets.token_hex(4)
            self._keys[kid] = (secret or secrets.token_bytes(32), None)
            self._current = kid
            return kid

    def signing(self) -> tuple[str, bytes]:
        with self._lock:
            if self._current is None:
                raise RuntimeError("el anillo no tiene llaves: usa add_key() o rotate()")
            return self._current, self._keys[self._current][0]

    def secret_for(self, kid: str) -> bytes | None:
        with self._lock:
            item = self._keys.get(kid)
            if not item or (item[1] is not None and self._clock() >= item[1]):
                return None
            return item[0]

    def purge(self) -> int:
        with self._lock:
            now = self._clock()
            dead = [k for k, (_, r) in self._keys.items() if r is not None and now >= r and k != self._current]
            for k in dead:
                del self._keys[k]
            return len(dead)


class TokenIssuer:
    def __init__(self, ring: SigningKeyRing, *, ttl: float = 3600.0, max_lifetime: float = 86_400.0,
                 issuer: str = "", clock: Callable[[], float] = time.time) -> None:
        if ttl <= 0 or max_lifetime < ttl:
            raise ValueError("ttl debe ser positivo y max_lifetime >= ttl")
        self.ring, self.ttl, self.max_lifetime, self.issuer, self._clock = ring, ttl, max_lifetime, issuer, clock

    def _sign(self, claims: dict) -> str:
        kid, secret = self.ring.signing()
        head = f"v1.{kid}.{_b64(json.dumps(claims, separators=(',', ':'), sort_keys=True).encode())}"
        return f"{head}.{_b64(hmac.new(secret, head.encode(), hashlib.sha256).digest())}"

    def issue(self, subject: str, *, scopes: tuple[str, ...] = (), ttl: float | None = None) -> str:
        now = self._clock()
        ttl = self.ttl if ttl is None else min(ttl, self.max_lifetime)
        if ttl <= 0:
            raise ValueError("ttl debe ser positivo")
        return self._sign({"sub": subject, "scp": list(scopes), "iat": int(now), "oiat": int(now),
                           "exp": int(now + ttl), "jti": secrets.token_hex(8), "iss": self.issuer})

    def verify(self, token: str, *, required_scope: str | None = None) -> dict:
        try:
            version, kid, payload, sig = token.split(".")
            if version != "v1":
                raise ValueError
            claims = json.loads(_unb64(payload))
            given = _unb64(sig)
        except (ValueError, AttributeError, TypeError):
            raise TokenInvalid("malformed") from None
        secret = self.ring.secret_for(kid)
        if secret is None:
            raise TokenInvalid("unknown_kid")
        expected = hmac.new(secret, f"v1.{kid}.{payload}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, given):
            raise TokenInvalid("bad_signature")
        if not isinstance(claims, dict) or not isinstance(claims.get("exp"), (int, float)):
            raise TokenInvalid("malformed")
        if self._clock() >= claims["exp"]:
            raise TokenInvalid("expired")
        if required_scope and required_scope not in claims.get("scp", []):
            raise TokenInvalid("scope")
        return claims

    def refresh(self, token: str, *, ttl: float | None = None) -> str:
        """Renueva un token VIGENTE con los mismos sub/scopes. Nunca supera `max_lifetime` desde la emisión original."""
        claims = self.verify(token)
        now = self._clock()
        origin = int(claims.get("oiat", claims.get("iat", now)))
        remaining = origin + self.max_lifetime - now
        if remaining <= 0:
            raise TokenInvalid("max_lifetime")
        ttl = min(self.ttl if ttl is None else ttl, remaining)
        return self._sign({"sub": claims["sub"], "scp": claims.get("scp", []), "iat": int(now), "oiat": origin,
                           "exp": int(now + ttl), "jti": secrets.token_hex(8), "iss": self.issuer})
