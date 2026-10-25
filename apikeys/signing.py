"""Firma de peticiones HMAC-SHA256 con perfiles configurables.

Cada proveedor (delivery, transporte, pagos...) define su propio texto canónico, codificación y cabeceras.
NO existe un esquema único: para DiDi u otro tercero, crea un SignatureProfile con las reglas EXACTAS de la
documentación oficial de su portal de partners. Los perfiles incluidos son patrones genéricos, no de un proveedor.

Importante: firma siempre los bytes EXACTOS del cuerpo que enviarás (no re-serialices el JSON después de firmar).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping
from urllib.parse import parse_qsl, quote, urlsplit


class SignatureError(ValueError):
    """Firma rechazada. `reason` es un código estable: missing, timestamp, signature, replay, unknown_key."""

    def __init__(self, reason: str):
        super().__init__("firma inválida")
        self.reason = reason


@dataclass(frozen=True)
class Parts:
    method: str
    path: str
    query: tuple[tuple[str, str], ...]
    body: bytes
    timestamp: str
    nonce: str
    key_id: str


def _quote(text: str) -> str:
    return quote(text, safe="-_.~")


def canonical_query(query) -> str:
    return "&".join(f"{_quote(k)}={_quote(v)}" for k, v in sorted(query))


def _standard(p: Parts) -> str:
    return "\n".join([p.method, p.path, canonical_query(p.query), hashlib.sha256(p.body).hexdigest(), p.timestamp, p.nonce])


def _sorted_params(p: Parts) -> str:
    pairs = sorted([*p.query, ("app_key", p.key_id), ("nonce", p.nonce), ("timestamp", p.timestamp)])
    return "&".join(f"{k}={v}" for k, v in pairs) + ("&" + p.body.decode("utf-8") if p.body else "")


def _body_dot_timestamp(p: Parts) -> str:
    return f"{p.timestamp}." + p.body.decode("utf-8")


@dataclass(frozen=True)
class SignatureProfile:
    name: str
    canonicalize: Callable[[Parts], str]
    encoding: str = "hex"          # "hex" (minúsculas) o "base64"
    timestamp_unit: str = "s"      # "s" o "ms"
    key_header: str = "X-Api-Key"
    timestamp_header: str = "X-Timestamp"
    nonce_header: str | None = "X-Nonce"
    signature_header: str = "X-Signature"

    def encode(self, digest: bytes) -> str:
        if self.encoding == "base64":
            return base64.b64encode(digest).decode("ascii")
        return digest.hex()


PROFILE_STANDARD = SignatureProfile("standard", _standard)
PROFILE_SORTED_PARAMS = SignatureProfile("sorted_params", _sorted_params, key_header="X-App-Key")
PROFILE_BODY_DOT_TIMESTAMP = SignatureProfile(
    "body_dot_timestamp", _body_dot_timestamp, key_header="X-Client-Id", nonce_header=None)


@dataclass(frozen=True)
class SignedRequest:
    signature: str
    timestamp: str
    nonce: str
    canonical: str
    headers: dict[str, str] = field(default_factory=dict)


def _parts(method: str, url: str, params, body, timestamp: str, nonce: str, key_id: str) -> Parts:
    split = urlsplit(url)
    query = list(parse_qsl(split.query, keep_blank_values=True)) + list((params or {}).items())
    raw = body.encode("utf-8") if isinstance(body, str) else bytes(body or b"")
    return Parts(method.upper(), split.path or "/", tuple((str(k), str(v)) for k, v in query), raw, timestamp, nonce, key_id)


def _digest(profile: SignatureProfile, secret: str, parts: Parts) -> tuple[str, str]:
    canonical = profile.canonicalize(parts)
    mac = hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).digest()
    return profile.encode(mac), canonical


class Signer:
    def __init__(self, key_id: str, secret: str, profile: SignatureProfile = PROFILE_STANDARD,
                 clock: Callable[[], float] = time.time) -> None:
        if not key_id or len(secret) < 16:
            raise ValueError("key_id obligatorio y secreto de al menos 16 caracteres")
        self.key_id, self._secret, self.profile, self._clock = key_id, secret, profile, clock

    def sign(self, method: str, url: str, *, body: bytes | str = b"", params: Mapping[str, str] | None = None,
             nonce: str | None = None) -> SignedRequest:
        now = self._clock()
        timestamp = str(int(now * 1000) if self.profile.timestamp_unit == "ms" else int(now))
        nonce = nonce or secrets.token_hex(8)
        signature, canonical = _digest(self.profile, self._secret, _parts(method, url, params, body, timestamp, nonce, self.key_id))
        p = self.profile
        headers = {p.key_header: self.key_id, p.timestamp_header: timestamp, p.signature_header: signature}
        if p.nonce_header:
            headers[p.nonce_header] = nonce
        return SignedRequest(signature, timestamp, nonce, canonical, headers)


class NonceCache:
    """Memoria acotada de nonces vistos (anti-replay)."""

    def __init__(self, ttl: float = 600.0, max_items: int = 10_000, clock: Callable[[], float] = time.time) -> None:
        self.ttl, self.max_items, self._clock = ttl, max_items, clock
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def add(self, key: str) -> bool:
        """True si es nuevo; False si ya se había visto (replay)."""
        now = self._clock()
        with self._lock:
            for k in [k for k, exp in self._seen.items() if exp <= now]:
                del self._seen[k]
            if key in self._seen:
                return False
            while len(self._seen) >= self.max_items:
                self._seen.pop(next(iter(self._seen)))
            self._seen[key] = now + self.ttl
            return True


class Verifier:
    """Valida peticiones firmadas entrantes (para tu propia API)."""

    def __init__(self, secret_lookup: Callable[[str], str | None], profile: SignatureProfile = PROFILE_STANDARD,
                 tolerance: float = 300.0, nonces: NonceCache | None = None, clock: Callable[[], float] = time.time) -> None:
        self.lookup, self.profile, self.tolerance, self._clock = secret_lookup, profile, tolerance, clock
        self.nonces = nonces or NonceCache(ttl=tolerance * 2, clock=clock)

    def verify(self, method: str, url: str, headers: Mapping[str, str], *, body: bytes | str = b"",
               params: Mapping[str, str] | None = None) -> str:
        p = self.profile
        h = {k.lower(): v for k, v in headers.items()}
        key_id = h.get(p.key_header.lower())
        timestamp = h.get(p.timestamp_header.lower())
        signature = h.get(p.signature_header.lower())
        nonce = h.get(p.nonce_header.lower(), "") if p.nonce_header else ""
        if not key_id or not timestamp or not signature or (p.nonce_header and not nonce):
            raise SignatureError("missing")
        try:
            sent = int(timestamp) / (1000 if p.timestamp_unit == "ms" else 1)
        except ValueError:
            raise SignatureError("timestamp") from None
        if abs(self._clock() - sent) > self.tolerance:
            raise SignatureError("timestamp")
        secret = self.lookup(key_id)
        if not secret:
            raise SignatureError("unknown_key")
        expected, _ = _digest(p, secret, _parts(method, url, params, body, timestamp, nonce, key_id))
        if not hmac.compare_digest(expected, signature):
            raise SignatureError("signature")
        if p.nonce_header and not self.nonces.add(f"{key_id}:{nonce}"):  # solo tras validar la firma
            raise SignatureError("replay")
        return key_id
