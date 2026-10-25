"""Cliente OAuth2 con refresco automático: client_credentials y refresh_token (con rotación del refresh token).

get_token() devuelve siempre un access token vigente: lo renueva `skew` segundos antes de que venza, con un
candado para que varios hilos no refresquen a la vez. Los tokens nunca se escriben en mensajes de error ni logs.
"""
from __future__ import annotations

import base64
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

Transport = Callable[[str, dict, dict, float], "tuple[int, dict]"]
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class TokenError(RuntimeError):
    pass


def urllib_transport(url: str, data: dict, headers: dict, timeout: float) -> tuple[int, dict]:
    """POST application/x-www-form-urlencoded -> (status, json). Lanza OSError si no hay conexión."""
    req = urllib.request.Request(
        url, data=urllib.parse.urlencode(data).encode("ascii"), method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read(1_000_000)
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read(1_000_000)
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        body = {}
    return status, body if isinstance(body, dict) else {}


class TokenManager:
    def __init__(self, token_url: str, client_id: str, client_secret: str | None = None, *,
                 scope: str | None = None, refresh_token: str | None = None, auth_style: str = "basic",
                 skew: float = 60.0, retries: int = 2, backoff: float = 0.5, timeout: float = 15.0,
                 default_ttl: float = 3600.0, transport: Transport = urllib_transport,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 on_update: Callable[[dict], None] | None = None, allow_insecure: bool = False) -> None:
        parsed = urllib.parse.urlsplit(token_url)
        if parsed.scheme != "https" and not (allow_insecure or (parsed.scheme == "http" and parsed.hostname in _LOCAL_HOSTS)):
            raise ValueError("token_url debe usar https")
        if auth_style not in ("basic", "body"):
            raise ValueError("auth_style debe ser 'basic' o 'body'")
        self.token_url, self.client_id, self.client_secret = token_url, client_id, client_secret
        self.scope, self.auth_style, self.skew, self.retries = scope, auth_style, skew, max(0, retries)
        self.backoff, self.timeout, self.default_ttl = backoff, timeout, default_ttl
        self._transport, self._clock, self._sleep, self._on_update = transport, clock, sleep, on_update
        self._refresh_token = refresh_token
        self._access: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------ público
    def get_token(self) -> str:
        with self._lock:
            if self._access and self._clock() < self._expires_at - self.skew:
                return self._access
            self._renew()
            return self._access  # type: ignore[return-value]

    def force_refresh(self) -> str:
        """Úsalo tras un 401 de la API: descarta el token actual y obtiene otro."""
        with self._lock:
            self._access, self._expires_at = None, 0.0
            self._renew()
            return self._access  # type: ignore[return-value]

    def authorization_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.get_token()}"}

    def seconds_left(self) -> float:
        return max(0.0, self._expires_at - self._clock()) if self._access else 0.0

    def state(self) -> dict:
        """Lo que conviene persistir entre reinicios (la rotación invalida el refresh token anterior)."""
        return {"refresh_token": self._refresh_token, "expires_at": self._expires_at}

    # ------------------------------------------------------------ interno
    def _post(self, data: dict) -> tuple[int, dict]:
        headers: dict[str, str] = {}
        if self.scope:
            data = {**data, "scope": self.scope}
        if self.auth_style == "basic" and self.client_secret:
            cred = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode("ascii")
            headers["Authorization"] = "Basic " + cred
        else:
            data = {**data, "client_id": self.client_id, **({"client_secret": self.client_secret} if self.client_secret else {})}
        last = "sin respuesta"
        for attempt in range(self.retries + 1):
            try:
                status, body = self._transport(self.token_url, data, headers, self.timeout)
            except (OSError, TimeoutError) as exc:
                last = f"error de red ({type(exc).__name__})"
            else:
                if status < 500 and status != 429:
                    return status, body
                last = f"el servidor respondió {status}"
            if attempt < self.retries:
                self._sleep(self.backoff * (2 ** attempt))
        raise TokenError(f"no se pudo obtener el token: {last}")

    def _renew(self) -> None:
        if self._refresh_token:
            status, body = self._post({"grant_type": "refresh_token", "refresh_token": self._refresh_token})
            if status == 200:
                return self._apply(body)
            if status not in (400, 401) or not self.client_secret:
                raise TokenError(f"el refresco falló ({status}: {body.get('error', 'sin detalle')})")
            self._refresh_token = None  # refresh token caducado o revocado: se cae a client_credentials
        if not self.client_secret:
            raise TokenError("no hay refresh token válido ni client_secret para pedir uno nuevo")
        status, body = self._post({"grant_type": "client_credentials"})
        if status != 200:
            raise TokenError(f"el servidor rechazó las credenciales ({status}: {body.get('error', 'sin detalle')})")
        self._apply(body)

    def _apply(self, body: dict) -> None:
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise TokenError("la respuesta no incluye access_token")
        try:
            ttl = float(body.get("expires_in", self.default_ttl))
        except (TypeError, ValueError):
            ttl = self.default_ttl
        if ttl <= 0:
            ttl = self.default_ttl
        self._access, self._expires_at = token, self._clock() + ttl
        rotated = body.get("refresh_token")
        if isinstance(rotated, str) and rotated:  # rotación: el nuevo reemplaza al anterior
            self._refresh_token = rotated
        if self._on_update:
            self._on_update(self.state())
