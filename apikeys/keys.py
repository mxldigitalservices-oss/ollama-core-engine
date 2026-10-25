"""API keys: generación con `secrets`, almacenamiento solo del hash HMAC-SHA256, TTL, revocación y rotación.

Formato del token entregado (se muestra una sola vez):  <prefijo>_<key_id>_<secreto>
En el almacén solo queda HMAC-SHA256(pepper, secreto); un volcado del almacén no revela claves usables.
El pepper vive fuera del almacén (variable de entorno APIKEYS_PEPPER, mínimo 16 caracteres).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from typing import Callable, NamedTuple

_PREFIX_RE = re.compile(r"^[a-z][a-z0-9]{1,11}$")
_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def hash_secret(secret: str, pepper: str) -> str:
    return hmac.new(pepper.encode("utf-8"), secret.encode("utf-8"), hashlib.sha256).hexdigest()


class ApiKey(NamedTuple):
    key_id: str
    secret: str
    token: str  # prefijo_id_secreto: muéstralo una vez y no lo guardes


@dataclass(frozen=True)
class KeyRecord:
    key_id: str
    name: str
    prefix: str
    secret_hash: str
    created_at: float
    expires_at: float | None = None
    scopes: tuple[str, ...] = ()
    revoked_at: float | None = None
    replaced_by: str | None = None


class MemoryStore:
    def __init__(self) -> None:
        self._items: dict[str, KeyRecord] = {}
        self._lock = threading.RLock()

    def get(self, key_id: str) -> KeyRecord | None:
        with self._lock:
            return self._items.get(key_id)

    def put(self, record: KeyRecord) -> None:
        with self._lock:
            self._items[record.key_id] = record

    def all(self) -> list[KeyRecord]:
        with self._lock:
            return list(self._items.values())

    def delete(self, key_id: str) -> None:
        with self._lock:
            self._items.pop(key_id, None)


class JsonFileStore(MemoryStore):
    """MemoryStore persistido en JSON (solo hashes). Escritura atómica y permisos 0600."""

    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = path
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                for raw in json.load(fh):
                    raw["scopes"] = tuple(raw.get("scopes", ()))
                    self._items[raw["key_id"]] = KeyRecord(**raw)

    def put(self, record: KeyRecord) -> None:
        with self._lock:
            self._items[record.key_id] = record
            self._flush()

    def delete(self, key_id: str) -> None:
        with self._lock:
            self._items.pop(key_id, None)
            self._flush()

    def _flush(self) -> None:
        data = json.dumps([asdict(r) for r in self._items.values()], indent=2)
        directory = os.path.dirname(os.path.abspath(self.path))
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".keys-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)


@dataclass
class KeyManager:
    pepper: str
    store: MemoryStore = field(default_factory=MemoryStore)
    clock: Callable[[], float] = time.time

    def __post_init__(self) -> None:
        if not isinstance(self.pepper, str) or len(self.pepper) < 16:
            raise ValueError("el pepper debe tener al menos 16 caracteres (usa APIKEYS_PEPPER)")

    # ---------------------------------------------------------- crear
    def create(self, name: str, *, prefix: str = "sk", ttl: float | None = None, scopes: tuple[str, ...] = ()) -> ApiKey:
        if not _PREFIX_RE.match(prefix):
            raise ValueError("prefijo inválido: 2-12 caracteres [a-z0-9], empezando por letra")
        if ttl is not None and ttl <= 0:
            raise ValueError("ttl debe ser positivo")
        key_id = secrets.token_hex(6)
        secret = secrets.token_urlsafe(32)  # 256 bits de entropía
        now = self.clock()
        self.store.put(KeyRecord(
            key_id=key_id, name=name, prefix=prefix, secret_hash=hash_secret(secret, self.pepper),
            created_at=now, expires_at=None if ttl is None else now + ttl, scopes=tuple(scopes)))
        return ApiKey(key_id, secret, f"{prefix}_{key_id}_{secret}")

    # ---------------------------------------------------------- verificar
    def verify(self, token: str, *, required_scope: str | None = None) -> KeyRecord | None:
        """Devuelve el registro si el token es válido; None en cualquier otro caso (sin pistas del motivo)."""
        parts = token.split("_", 2) if isinstance(token, str) else []
        if len(parts) != 3 or not _PREFIX_RE.match(parts[0]) or not _ID_RE.match(parts[1]):
            return None
        prefix, key_id, secret = parts
        record = self.store.get(key_id)
        candidate = hash_secret(secret, self.pepper)
        expected = record.secret_hash if record else "0" * 64  # comparación constante también si no existe
        match = hmac.compare_digest(candidate, expected)
        if not record or not match or record.prefix != prefix:
            return None
        now = self.clock()
        if record.revoked_at is not None or (record.expires_at is not None and now >= record.expires_at):
            return None
        if required_scope and required_scope not in record.scopes:
            return None
        return record

    # ---------------------------------------------------------- ciclo de vida
    def revoke(self, key_id: str) -> bool:
        record = self.store.get(key_id)
        if not record or record.revoked_at is not None:
            return False
        self.store.put(replace(record, revoked_at=self.clock()))
        return True

    def rotate(self, key_id: str, *, grace: float = 3600.0, ttl: float | None = None) -> ApiKey:
        """Emite una clave nueva con el mismo nombre/alcances; la anterior sigue válida `grace` segundos."""
        old = self.store.get(key_id)
        if not old or old.revoked_at is not None:
            raise KeyError("clave inexistente o revocada")
        if grace < 0:
            raise ValueError("grace no puede ser negativo")
        new = self.create(old.name, prefix=old.prefix, ttl=ttl, scopes=old.scopes)
        deadline = self.clock() + grace
        cutoff = deadline if old.expires_at is None else min(old.expires_at, deadline)
        self.store.put(replace(old, expires_at=cutoff, replaced_by=new.key_id))
        return new

    def purge_expired(self) -> int:
        """Borra del almacén las claves vencidas o revocadas. Devuelve cuántas."""
        now, removed = self.clock(), 0
        for record in self.store.all():
            if record.revoked_at is not None or (record.expires_at is not None and now >= record.expires_at):
                self.store.delete(record.key_id)
                removed += 1
        return removed
