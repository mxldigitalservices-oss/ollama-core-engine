"""Memoria ligera y contextual en carpetas locales de conocimiento (solo biblioteca estándar).

Qué se guarda (lo esencial, nunca el código completo):
  projects/<repo>/context.json  stack, resumen, última tarea, variables de entorno y MAPA de archivos
                                (ruta, tamaño y nombres de funciones/clases) del proyecto en curso.
  projects/<repo>/lessons.json  errores de validación que ya ocurrieron y se resolvieron (con contador).
  refs/<hash>.json              caché de consultas web (paquetes, búsquedas, documentación) con caducidad.

Qué se descarta (purga automática al arrancar, tras cada tarea y bajo demanda):
  - proyectos inactivos más de AGENT_MEMORY_TTL_DAYS (el proyecto activo se conserva);
  - referencias web caducadas (AGENT_WEB_CACHE_TTL);
  - lo más antiguo cuando se supera AGENT_MEMORY_MAX_BYTES (primero referencias, luego proyectos inactivos);
  - dentro de un proyecto: lecciones poco útiles (tope) y archivos que ya no existen o exceden el tope.
Todo texto guardado se redacta de secretos. La memoria nunca bloquea una tarea: ante cualquier fallo se ignora.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
from typing import Callable

from . import config
from .validators import SECRET_PATTERNS

_ISSUE_RE = re.compile(r"^\[(?P<kind>[\w-]+)\]\s+(?:\S+?(?::\d+)?:\s+)?(?P<msg>.*)$", re.S)
_SKIP_LESSON_KINDS = {"límite", "formato", "revisión", "ruta"}
_TECH = {
    "fastapi": "FastAPI", "flask": "Flask", "django": "Django", "express": "Express", "react": "React",
    "vite": "Vite", "tailwindcss": "TailwindCSS", "sqlalchemy": "SQLAlchemy", "psycopg2-binary": "PostgreSQL",
    "psycopg2": "PostgreSQL", "asyncpg": "PostgreSQL", "pg": "PostgreSQL", "pymongo": "MongoDB", "mongoose": "MongoDB",
    "redis": "Redis", "mysql-connector-python": "MySQL", "mysql2": "MySQL", "pydantic": "Pydantic", "ejs": "EJS",
}
_ENV_RES = (re.compile(r"os\.(?:getenv|environ\.get)\(\s*[\"']([A-Z][A-Z0-9_]{2,})"),
            re.compile(r"os\.environ\[\s*[\"']([A-Z][A-Z0-9_]{2,})"),
            re.compile(r"process\.env\.([A-Z][A-Z0-9_]{2,})"))


def redact(text: str) -> str:
    for pattern, _ in SECRET_PATTERNS:
        text = pattern.sub("[REDACTADO]", text)
    for secret in (config.API_TOKEN, config.GITHUB_TOKEN, config.BRAVE_API_KEY):
        if secret and len(secret) >= 8:
            text = text.replace(secret, "[REDACTADO]")
    return text


def slug(repo: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "__", repo).strip("._")[:100] or "repo"


def _write_json(path: str, data) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _dir_size(path: str) -> int:
    total = 0
    for base, _, names in os.walk(path):
        for n in names:
            try:
                total += os.path.getsize(os.path.join(base, n))
            except OSError:
                pass
    return total


def symbols(rel: str, content: str, limit: int = 12) -> list[str]:
    """Nombres relevantes del archivo (para el mapa del proyecto); barato y sin ejecutar nada."""
    ext = os.path.splitext(rel)[1].lower()
    names: list[str] = []
    if ext == ".py":
        try:
            tree = ast.parse(content)
        except (SyntaxError, ValueError, RecursionError):
            return []
        names = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    elif ext in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
        names = re.findall(r"(?:function|class)\s+([A-Za-z_$][\w$]*)|(?:const|let)\s+([A-Z][A-Za-z0-9_$]*)\s*=", content)
        names = [a or b for a, b in names]
    elif ext in (".html", ".ejs"):
        m = re.search(r"<title>(.*?)</title>", content, re.S | re.I)
        names = [re.sub(r"\s+", " ", m.group(1)).strip()[:60]] if m else []
    return list(dict.fromkeys(names))[:limit]


def detect_stack(files: dict[str, str]) -> list[str]:
    stack: list[str] = []
    exts = {os.path.splitext(p)[1].lower() for p in files}
    for ext, label in ((".py", "Python"), (".ts", "TypeScript"), (".tsx", "TypeScript"), (".js", "JavaScript"),
                       (".jsx", "JavaScript"), (".html", "HTML"), (".css", "CSS"), (".sql", "SQL")):
        if ext in exts:
            stack.append(label)
    if any(os.path.basename(p).lower() == "dockerfile" for p in files):
        stack.append("Docker")
    for path, content in files.items():
        base = os.path.basename(path)
        names: list[str] = []
        if re.fullmatch(r"requirements[\w.-]*\.txt", base):
            names = [re.split(r"[\s<>=!~;\[]", ln.split("#")[0].strip(), 1)[0].lower() for ln in content.splitlines() if ln.strip()]
        elif base == "package.json":
            try:
                data = json.loads(content)
                names = [*data.get("dependencies", {}), *data.get("devDependencies", {})]
            except (ValueError, AttributeError):
                names = []
        stack += [_TECH[n] for n in names if n in _TECH]
    return list(dict.fromkeys(stack))


def lesson_from(issue: str) -> tuple[str, str] | None:
    """'[kind] ruta:línea: mensaje' -> (clave, texto) sin rutas ni líneas; None si no aporta."""
    m = _ISSUE_RE.match(issue.strip())
    if not m or m.group("kind") in _SKIP_LESSON_KINDS:
        return None
    msg = redact(re.sub(r"\s+", " ", m.group("msg"))).strip()[:160]
    return (f"{m.group('kind')}:{msg.lower()}", f"[{m.group('kind')}] {msg}") if len(msg) > 8 else None


class ProjectMemory:
    def __init__(self, manager: "Memory", repo: str) -> None:
        self.m, self.repo = manager, repo
        self.dir = os.path.join(manager.root, "projects", slug(repo))

    def _ctx(self) -> dict:
        return _read_json(os.path.join(self.dir, "context.json"), {})

    def lessons(self) -> list[dict]:
        return _read_json(os.path.join(self.dir, "lessons.json"), [])

    # ------------------------------------------------------------ lectura (para el prompt)
    def prompt(self, instruction: str, limit: int | None = None) -> str:
        ctx, lessons = self._ctx(), self.lessons()
        if not ctx and not lessons:
            return ""
        limit = limit or config.MEMORY_PROMPT_CHARS
        lines = ["MEMORIA DEL PROYECTO (contexto activo guardado; es un resumen, no el código):"]
        if ctx.get("stack"):
            lines.append("Stack: " + ", ".join(ctx["stack"]))
        if ctx.get("summary"):
            lines.append("Resumen: " + ctx["summary"])
        if ctx.get("last_instruction"):
            lines.append("Última tarea: " + ctx["last_instruction"])
        if ctx.get("env"):
            lines.append("Variables de entorno usadas: " + ", ".join(ctx["env"][:15]))
        words = {w for w in re.findall(r"[a-záéíóúñ0-9_]{4,}", instruction.lower())}
        ranked = sorted(ctx.get("files", {}).items(),
                        key=lambda kv: (-sum(w in (kv[0] + " " + " ".join(kv[1].get("sym", []))).lower() for w in words), -kv[1].get("t", 0)))
        if ranked:
            lines.append("Mapa de archivos (más relevantes primero):")
            for path, info in ranked:
                lines.append(f"- {path}" + (f" [{', '.join(info['sym'])}]" if info.get("sym") else ""))
        top = sorted(lessons, key=lambda x: (-x.get("n", 1), -x.get("last", 0)))[:3]
        if top:
            lines.append("Errores que ya ocurrieron aquí (evítalos):")
            lines += [f"- {x['text']}" for x in top]
        out = ""
        for ln in lines:
            if len(out) + len(ln) + 1 > limit:
                break
            out += ln + "\n"
        return out.strip()

    # ------------------------------------------------------------ escritura (al terminar bien)
    def record(self, task: dict, files: dict[str, str], root: str | None = None, errors: list[str] | None = None,
               plan: dict | None = None) -> None:
        now = self.m.clock()
        ctx = self._ctx() or {"repo": self.repo, "created": now, "tasks": 0, "files": {}}
        stack = detect_stack(files) + list(ctx.get("stack", []))
        ctx["stack"] = list(dict.fromkeys(stack))[:10]
        if plan and plan.get("summary"):
            ctx["summary"] = redact(plan["summary"])[:300]
        ctx["last_instruction"] = redact(re.sub(r"\s+", " ", task.get("instruction", ""))).strip()[:300]
        ctx["tasks"] = ctx.get("tasks", 0) + 1
        ctx["updated"] = now
        env = list(ctx.get("env", [])) + list(plan.get("env", []) if plan else [])
        for content in files.values():
            for rx in _ENV_RES:
                env += rx.findall(content)
        ctx["env"] = list(dict.fromkeys(env))[:30]
        index = ctx["files"]
        for rel, content in files.items():
            index[rel] = {"b": len(content.encode("utf-8")), "t": now, "sym": symbols(rel, content)}
        if root:  # el mapa solo describe lo que existe
            for rel in [r for r in index if not os.path.exists(os.path.join(root, r))]:
                del index[rel]
        for rel in sorted(index, key=lambda r: index[r].get("t", 0))[:max(0, len(index) - config.MEMORY_MAX_FILES)]:
            del index[rel]
        _write_json(os.path.join(self.dir, "context.json"), ctx)
        self._learn(errors or [], now)
        self.m.set_active(self.repo)

    def _learn(self, errors: list[str], now: float) -> None:
        learned = [x for x in (lesson_from(e) for e in errors) if x]
        if not learned:
            return
        table = {x["key"]: x for x in self.lessons()}
        for key, text in learned:
            item = table.setdefault(key, {"key": key, "text": text, "n": 0, "last": now})
            item["n"] += 1
            item["last"] = now
        keep = sorted(table.values(), key=lambda x: (x["n"], x["last"]), reverse=True)[:config.MEMORY_MAX_LESSONS]
        _write_json(os.path.join(self.dir, "lessons.json"), keep)


class Memory:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self._lock = threading.Lock()

    @property
    def root(self) -> str:
        return config.KNOWLEDGE_DIR

    def project(self, repo: str) -> ProjectMemory:
        return ProjectMemory(self, repo)

    # ---- proyecto activo
    def set_active(self, repo: str) -> None:
        _write_json(os.path.join(self.root, "active.json"), {"repo": repo, "at": self.clock()})

    def active(self) -> str:
        return _read_json(os.path.join(self.root, "active.json"), {}).get("repo", "")

    # ---- caché de referencias web (la usa web.Research)
    def get_ref(self, key: str):
        path = os.path.join(self.root, "refs", hashlib.sha1(key.encode()).hexdigest() + ".json")
        item = _read_json(path, None)
        if not item or self.clock() - item.get("at", 0) > config.WEB_CACHE_TTL:
            return None
        return item.get("v")

    def put_ref(self, key: str, value) -> None:
        try:
            _write_json(os.path.join(self.root, "refs", hashlib.sha1(key.encode()).hexdigest() + ".json"),
                        {"at": self.clock(), "v": value})
        except OSError:
            pass

    # ---- purga
    def purge(self) -> dict:
        """Descarta lo caducado y recorta al tope de tamaño. Devuelve contadores."""
        out = {"expired_projects": 0, "expired_refs": 0, "evicted": 0}
        with self._lock:
            now, active = self.clock(), self.active_slug()
            ttl = config.MEMORY_PROJECT_TTL_DAYS * 86400
            projects = os.path.join(self.root, "projects")
            for name in self._listdir(projects):
                path = os.path.join(projects, name)
                updated = _read_json(os.path.join(path, "context.json"), {}).get("updated") or self._mtime(path)
                if name != active and now - updated > ttl:
                    shutil.rmtree(path, ignore_errors=True)
                    out["expired_projects"] += 1
            refs = os.path.join(self.root, "refs")
            for name in self._listdir(refs):
                path = os.path.join(refs, name)
                if now - _read_json(path, {}).get("at", self._mtime(path)) > config.WEB_CACHE_TTL:
                    self._rm(path)
                    out["expired_refs"] += 1
            # tope de tamaño: primero referencias, luego proyectos inactivos (los más antiguos primero)
            while _dir_size(self.root) > config.MEMORY_MAX_BYTES:
                victims = sorted((os.path.join(refs, n) for n in self._listdir(refs)), key=self._mtime)
                victims += sorted((os.path.join(projects, n) for n in self._listdir(projects) if n != active), key=self._mtime)
                if not victims:
                    break
                for path in victims[:max(1, len(victims) // 4)]:
                    self._rm(path)
                    out["evicted"] += 1
        return out

    def forget(self, repo: str | None = None) -> int:
        """Borra la memoria de un proyecto (o toda si repo es None). Devuelve cuántos proyectos."""
        with self._lock:
            if repo is None:
                n = len(self._listdir(os.path.join(self.root, "projects")))
                shutil.rmtree(self.root, ignore_errors=True)
                return n
            path = os.path.join(self.root, "projects", slug(repo))
            existed = os.path.isdir(path)
            shutil.rmtree(path, ignore_errors=True)
            if self.active() == repo:
                self._rm(os.path.join(self.root, "active.json"))
            return int(existed)

    def stats(self) -> dict:
        projects = os.path.join(self.root, "projects")
        active = self.active_slug()
        items = []
        for name in self._listdir(projects):
            path = os.path.join(projects, name)
            ctx = _read_json(os.path.join(path, "context.json"), {})
            items.append({"repo": ctx.get("repo", name), "files": len(ctx.get("files", {})), "tasks": ctx.get("tasks", 0),
                          "lessons": len(_read_json(os.path.join(path, "lessons.json"), [])), "bytes": _dir_size(path),
                          "updated": ctx.get("updated"), "active": name == active})
        return {"enabled": config.MEMORY_ENABLED, "projects": sorted(items, key=lambda i: i["updated"] or 0, reverse=True),
                "refs": len(self._listdir(os.path.join(self.root, "refs"))), "bytes": _dir_size(self.root),
                "max_bytes": config.MEMORY_MAX_BYTES, "ttl_days": config.MEMORY_PROJECT_TTL_DAYS}

    # ---- utilidades
    def active_slug(self) -> str:
        repo = self.active()
        return slug(repo) if repo else ""

    @staticmethod
    def _listdir(path: str) -> list[str]:
        try:
            return os.listdir(path)
        except OSError:
            return []

    @staticmethod
    def _mtime(path: str) -> float:
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0.0

    @staticmethod
    def _rm(path: str) -> None:
        try:
            shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
        except OSError:
            pass


_MANAGER = Memory()


def manager() -> Memory:
    return _MANAGER


def for_repo(repo: str) -> ProjectMemory | None:
    """Memoria del proyecto, o None si está desactivada."""
    return _MANAGER.project(repo) if config.MEMORY_ENABLED else None
