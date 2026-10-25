"""Acceso web acotado para el agente (solo biblioteca estándar).

Principios:
- El contenido de la web es DATO NO CONFIABLE: se limita, se sanea y se entrega delimitado al modelo.
- Solo se DESCARGAN páginas de dominios permitidos (allowlist), por https y con IPs públicas (anti-SSRF,
  también en cada redirección). Los resultados de búsqueda aportan título y extracto sin descargar nada.
- Cada tarea tiene presupuesto de búsquedas, descargas y consultas de paquetes, y un corte automático si
  no hay conexión (el motor sigue sin web en lugar de esperar timeouts).
- Las consultas se redactan de secretos antes de salir del servidor.
"""
from __future__ import annotations

import html
import ipaddress
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from typing import Callable

from . import config
from .validators import SECRET_PATTERNS

UA = "ollama-core-engine/1.0 (agente de documentación)"
PYPI_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?$")
NPM_NAME_RE = re.compile(r"^(?:@[a-z0-9~-][a-z0-9._~-]*/)?[a-z0-9~-][a-z0-9._~-]*$")

# (url, headers, max_bytes, guarded) -> (status, body, final_url)
Transport = Callable[[str, dict, int, bool], "tuple[int, bytes, str]"]
_resolve = socket.getaddrinfo  # sustituible en pruebas


class WebError(Exception):
    pass


# ------------------------------------------------------------------ seguridad de URLs

def host_allowed(host: str, domains: list[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in (x.lower() for x in domains))


def check_fetch_url(url: str, domains: list[str]) -> str:
    """Valida una URL que se va a descargar. Devuelve el host o lanza WebError."""
    try:
        p = urllib.parse.urlsplit(url)
        port = p.port
    except ValueError:
        raise WebError("URL inválida") from None
    if p.scheme != "https":
        raise WebError("solo https")
    if p.username or p.password:
        raise WebError("la URL no puede llevar credenciales")
    if port not in (None, 443):
        raise WebError("puerto no permitido")
    host = (p.hostname or "").lower()
    if not host or not host_allowed(host, domains):
        raise WebError(f"dominio fuera de la lista permitida: {host or '?'}")
    try:
        infos = _resolve(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise WebError(f"no se pudo resolver {host}") from exc
    if not infos:
        raise WebError(f"no se pudo resolver {host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:  # privadas, loopback, link-local, metadatos de nube...
            raise WebError("el dominio resuelve a una dirección no pública")
    return host


class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, domains: list[str]) -> None:
        self.domains = domains

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        check_fetch_url(newurl, self.domains)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def urllib_get(url: str, headers: dict, max_bytes: int, guarded: bool) -> tuple[int, bytes, str]:
    opener = urllib.request.build_opener(_GuardedRedirect(config.WEB_ALLOWED_DOMAINS)) if guarded else urllib.request.build_opener()
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "identity", **headers})
    try:
        with opener.open(req, timeout=config.WEB_TIMEOUT) as resp:
            return resp.status, resp.read(max_bytes), resp.geturl()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(min(max_bytes, 200_000)), url


# ------------------------------------------------------------------ saneamiento

_TAG_RE = re.compile(r"<[^>]+>")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def sanitize_ref(text: str, limit: int) -> str:
    """Texto externo -> texto inocuo: sin etiquetas, sin marcadores del formato del agente, acotado."""
    text = html.unescape(_TAG_RE.sub(" ", text or ""))
    text = _CTRL_RE.sub("", text)
    text = re.sub(r"</?\s*(analisis|think)\s*>", "", text, flags=re.I)
    text = text.replace("===", "==").replace("```", "'''")
    return re.sub(r"\s+", " ", text).strip()[:limit]


def sanitize_query(text: str, limit: int = 120) -> str:
    """Quita de la consulta todo lo que no deba salir del servidor (secretos, URLs, rutas, correos, tokens)."""
    for pattern, _ in SECRET_PATTERNS:
        text = pattern.sub(" ", text)
    for secret in (config.API_TOKEN, config.GITHUB_TOKEN, config.BRAVE_API_KEY):
        if secret and len(secret) >= 8:
            text = text.replace(secret, " ")
    text = re.sub(r"https?://\S+|\S+@\S+\.\S+", " ", text)
    text = re.sub(r"(?:[\w.-]*/){2,}[\w.-]*", " ", text)            # rutas
    text = re.sub(r"\b[A-Za-z0-9_\-+/=]{24,}\b", " ", text)          # tokens y hashes largos
    text = re.sub(r"[^\w\s.'\-:()]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()[:limit]


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "nav", "header", "footer", "svg", "noscript", "form", "aside"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.depth += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.depth:
            self.depth -= 1

    def handle_data(self, data):
        if not self.depth and data.strip():
            self.parts.append(data.strip())


def html_to_text(raw: bytes, limit: int = 3000) -> str:
    p = _TextExtractor()
    try:
        p.feed(raw.decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 - html.parser es tolerante; por si acaso
        return ""
    return sanitize_ref(" ".join(p.parts), limit)


# ------------------------------------------------------------------ proveedores de búsqueda

@dataclass
class Ref:
    title: str
    url: str
    snippet: str


def _safe_url(url: str) -> str:
    return url if urllib.parse.urlsplit(url).scheme in ("http", "https") else ""


def parse_searxng(body: bytes) -> list[Ref]:
    data = json.loads(body)
    return [Ref(sanitize_ref(r.get("title", ""), 150), _safe_url(r.get("url", "")), sanitize_ref(r.get("content", ""), 400))
            for r in data.get("results", []) if isinstance(r, dict)]


def parse_brave(body: bytes) -> list[Ref]:
    data = json.loads(body)
    items = (data.get("web") or {}).get("results", [])
    return [Ref(sanitize_ref(r.get("title", ""), 150), _safe_url(r.get("url", "")), sanitize_ref(r.get("description", ""), 400))
            for r in items if isinstance(r, dict)]


_DDG_LINK = re.compile(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>', re.S)
_DDG_SNIP = re.compile(r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(?P<snip>.*?)</a>', re.S)


def parse_duckduckgo(body: bytes) -> list[Ref]:
    text = body.decode("utf-8", errors="replace")
    links, snippets = list(_DDG_LINK.finditer(text)), [m.group("snip") for m in _DDG_SNIP.finditer(text)]
    out: list[Ref] = []
    for i, m in enumerate(links):
        href = html.unescape(m.group("href"))
        if href.startswith("//"):
            href = "https:" + href
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query)
        if "uddg" in q:
            href = q["uddg"][0]
        if "duckduckgo.com/y.js" in href:  # anuncios
            continue
        out.append(Ref(sanitize_ref(m.group("title"), 150), _safe_url(href), sanitize_ref(snippets[i] if i < len(snippets) else "", 400)))
    return out


def resolve_provider() -> str:
    p = config.SEARCH_PROVIDER
    if p == "none":
        return "none"
    if p in ("searxng", "brave", "duckduckgo"):
        return p
    if config.SEARXNG_URL:
        return "searxng"
    if config.BRAVE_API_KEY:
        return "brave"
    return "duckduckgo"


def status() -> dict:
    return {"enabled": config.WEB_ENABLED, "provider": resolve_provider() if config.WEB_ENABLED else "none",
            "max_searches": config.WEB_MAX_SEARCHES, "allowed_domains": config.WEB_ALLOWED_DOMAINS}


# ------------------------------------------------------------------ paquetes

@dataclass
class Package:
    ecosystem: str
    name: str
    exists: bool | None          # None = no se pudo verificar (sin red)
    version: str = ""
    releases: int = 0
    age_days: int | None = None
    trusted: bool = False
    reason: str = ""

    def describe(self) -> str:
        age = f", {self.age_days} días" if self.age_days is not None else ""
        return f"{self.name} {self.version} ({self.releases} versiones{age})".strip()


def _iso_to_epoch(text: str) -> float | None:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ------------------------------------------------------------------ investigación por tarea

class Research:
    """Presupuesto y caché de la actividad web de UNA tarea. Los hallazgos van a task['web'] (visibles en la UI)."""

    def __init__(self, task: dict, step: Callable[[str], None], *, transport: Transport | None = None,
                 cache=None, clock: Callable[[], float] = time.time) -> None:
        self.refs: list[dict] = task.setdefault("web", [])
        self.step, self.cache, self.clock = step, cache, clock
        self.transport = transport or urllib_get
        self.searches = self.fetches = self.lookups = self.failures = 0
        self.offline = False
        self._lock = threading.Lock()
        self._memo: dict[tuple, object] = {}

    # ---- utilidades
    def can_search(self) -> bool:
        return not self.offline and resolve_provider() != "none" and self.searches < config.WEB_MAX_SEARCHES

    def _note(self, kind: str, title: str, url: str = "", note: str = "") -> None:
        with self._lock:
            if len(self.refs) < 40:
                self.refs.append({"kind": kind, "title": title[:150], "url": url if url.startswith("https://") else "", "note": note[:200]})

    def _get(self, url: str, headers: dict | None = None, max_bytes: int = 2_000_000, guarded: bool = False):
        """(status, body, final_url) o None si falla la red. Dos fallos de red seguidos apagan la web de la tarea."""
        if self.offline:
            return None
        try:
            res = self.transport(url, headers or {}, max_bytes, guarded)
        except (OSError, TimeoutError, WebError):
            with self._lock:
                self.failures += 1
                if self.failures >= 2 and not self.offline:
                    self.offline = True
                    self.step("sin conexión web: continúo sin ella")
            return None
        with self._lock:
            self.failures = 0
        return res

    def _cached(self, key: str):
        return self.cache.get_ref(key) if self.cache else None

    def _store(self, key: str, value) -> None:
        if self.cache:
            self.cache.put_ref(key, value)

    # ---- paquetes
    def package(self, ecosystem: str, name: str) -> Package:
        memo_key = ("pkg", ecosystem, name.lower())
        if memo_key in self._memo:
            return self._memo[memo_key]  # type: ignore[return-value]
        pkg = self._package(ecosystem, name)
        if pkg.exists is not None:
            self._memo[memo_key] = pkg
        return pkg

    def _package(self, ecosystem: str, name: str) -> Package:
        valid = (PYPI_NAME_RE if ecosystem == "pypi" else NPM_NAME_RE).match(name)
        if not valid:
            return Package(ecosystem, name, False, reason="nombre inválido")
        cache_key = f"pkg:{ecosystem}:{name.lower()}"
        hit = self._cached(cache_key)
        if hit:
            return Package(**hit)
        with self._lock:
            if self.lookups >= config.WEB_MAX_PACKAGE_LOOKUPS:
                return Package(ecosystem, name, None, reason="presupuesto de consultas agotado")
            self.lookups += 1
        if ecosystem == "pypi":
            url, headers = f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json", {"Accept": "application/json"}
        else:
            url = "https://registry.npmjs.org/" + urllib.parse.quote(name, safe="@")  # @scope/nombre -> @scope%2Fnombre
            headers = {"Accept": "application/vnd.npm.install-v1+json"}
        res = self._get(url, headers, max_bytes=8_000_000)
        if res is None:
            return Package(ecosystem, name, None, reason="sin conexión")
        status, body, _ = res
        if status == 404:
            pkg = Package(ecosystem, name, False, reason="no existe")
        elif status != 200:
            return Package(ecosystem, name, None, reason=f"HTTP {status}")
        else:
            try:
                pkg = self._parse_package(ecosystem, name, json.loads(body))
            except (ValueError, KeyError, TypeError):
                return Package(ecosystem, name, None, reason="respuesta ilegible")
        self._store(cache_key, pkg.__dict__)
        return pkg

    def _parse_package(self, ecosystem: str, name: str, data: dict) -> Package:
        if ecosystem == "pypi":
            released = {v: files for v, files in data.get("releases", {}).items() if files and not all(f.get("yanked") for f in files)}
            uploads = [t for files in released.values() for f in files if (t := _iso_to_epoch(f.get("upload_time_iso_8601", "")))]
            age = int((self.clock() - min(uploads)) / 86400) if uploads else None
            version, n = data["info"]["version"], len(released)
            trusted = n >= config.PACKAGE_MIN_RELEASES and (age or 0) >= config.PACKAGE_MIN_AGE_DAYS
        else:
            n = len(data.get("versions", {}))
            version = (data.get("dist-tags") or {}).get("latest", "")
            age, trusted = None, n >= config.PACKAGE_MIN_RELEASES  # el formato abreviado no trae fechas
        return Package(ecosystem, name, True, version, n, age, trusted,
                       "" if trusted else "historial insuficiente")

    def packages(self, ecosystem: str, names: list[str]) -> dict[str, Package]:
        """Consulta varios paquetes en paralelo (acotado)."""
        from concurrent.futures import ThreadPoolExecutor
        names = list(dict.fromkeys(names))
        if not names:
            return {}
        with ThreadPoolExecutor(max_workers=min(8, len(names))) as pool:
            return dict(zip(names, pool.map(lambda n: self.package(ecosystem, n), names)))

    # ---- búsqueda y documentación
    def search(self, query: str, limit: int = 4) -> list[Ref]:
        query = sanitize_query(query)
        if len(query) < 6 or not self.can_search():
            return []
        key = f"search:{resolve_provider()}:{query.lower()}"
        hit = self._cached(key)
        if hit is not None:
            refs = [Ref(**r) for r in hit]
        else:
            with self._lock:
                self.searches += 1
            refs = self._search(query)
            if refs:
                self._store(key, [r.__dict__ for r in refs])
        refs = [r for r in refs if r.url][:limit]
        for r in refs:
            self._note("búsqueda", r.title, r.url, query)
        if refs:
            self.step(f"búsqueda web: '{query}' ({len(refs)} resultado(s))")
        return refs

    def _search(self, query: str) -> list[Ref]:
        provider, q = resolve_provider(), urllib.parse.quote_plus(query)
        try:
            if provider == "searxng":
                res = self._get(f"{config.SEARXNG_URL}/search?q={q}&format=json&safesearch=1", {"Accept": "application/json"})
                return parse_searxng(res[1]) if res and res[0] == 200 else []
            if provider == "brave":
                res = self._get(f"https://api.search.brave.com/res/v1/web/search?q={q}&count=5",
                                {"Accept": "application/json", "X-Subscription-Token": config.BRAVE_API_KEY})
                return parse_brave(res[1]) if res and res[0] == 200 else []
            res = self._get(f"https://html.duckduckgo.com/html/?q={q}", {"Accept": "text/html"})  # mejor esfuerzo
            return parse_duckduckgo(res[1]) if res and res[0] == 200 else []
        except (ValueError, TypeError):
            return []

    def fetch(self, url: str, limit: int = 1800) -> str:
        """Descarga una página de la allowlist y devuelve su texto saneado ('' si no procede)."""
        with self._lock:
            if self.offline or self.fetches >= config.WEB_MAX_FETCHES:
                return ""
            try:
                check_fetch_url(url, config.WEB_ALLOWED_DOMAINS)
            except WebError:
                return ""
            self.fetches += 1
        key = "page:" + url
        hit = self._cached(key)
        if hit is not None:
            return hit
        res = self._get(url, {"Accept": "text/html"}, max_bytes=400_000, guarded=True)
        if not res or res[0] != 200:
            return ""
        text = html_to_text(res[1], limit)
        if text:
            self._store(key, text)
            self._note("documento", url.split("//", 1)[-1][:100], url)
        return text
