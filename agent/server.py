"""API local del agente (127.0.0.1) + dashboard web. Caddy la expone con el mismo Bearer token.

GET  /ui                -> dashboard (HTML/JS estático, sin secretos; no exige token)
POST /agent/task        -> 202 {"id": "..."}   encola una tarea (instruction, model, mode, repo, dry_run...)
GET  /agent/task/<id>   -> estado, pasos, razonamiento, validación y resultado
GET  /agent/tasks       -> últimas tareas
GET  /agent/config      -> configuración visible y checklist de variables (nunca valores secretos)
GET  /agent/models      -> modelos instalados y sugeridos
GET  /agent/memory      -> estado de la memoria ligera (proyectos, tamaño, lecciones)
POST /agent/memory/purge-> {} purga lo caducado | {"repo": "o/n"} olvida un proyecto | {"all": true} borra todo
GET  /agent/health      -> estado del servicio
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import queue
import re
import sys
import threading
import time
import uuid
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, engine
from . import memory, web
from .gitops import GitError, Workspace
from .llm import installed_models

log = logging.getLogger("agent")

MAX_BODY = 64 * 1024
MAX_TASKS = 50
TASKS: "OrderedDict[str, dict]" = OrderedDict()
TASKS_LOCK = threading.Lock()
QUEUE: "queue.Queue[dict]" = queue.Queue()


UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
STATIC = {
    "/ui": ("index.html", "text/html; charset=utf-8"),
    "/ui/index.html": ("index.html", "text/html; charset=utf-8"),
    "/ui/app.js": ("app.js", "application/javascript; charset=utf-8"),
}
CSP = ("default-src 'self'; script-src 'self' https://cdn.tailwindcss.com; style-src 'self' 'unsafe-inline'; "
       "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
MODEL_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,100}$")

PUBLIC_KEYS = ("id", "status", "repo", "model", "mode", "dry_run", "instruction", "created", "finished",
               "steps", "reasoning", "validation", "plan", "web", "memory", "result", "error")


def _public(task: dict) -> dict:
    out = {k: task.get(k) for k in PUBLIC_KEYS}
    result = out.get("result")
    if isinstance(result, dict) and "contents" in result:  # dry_run: el código se entrega aparte y acotado
        out["result"] = {**result, "contents": {p: c[: config.MAX_FILE_BYTES] for p, c in result["contents"].items()}}
    return out


def _env_checklist() -> list[dict]:
    """Variables que el servicio necesita; solo informa si están definidas, nunca su valor."""
    def row(name, required, is_set, help_):
        return {"name": name, "required": required, "set": bool(is_set), "help": help_}
    return [
        row("API_TOKEN", True, config.API_TOKEN, "Token Bearer que protege Caddy y esta API (openssl rand -hex 32)."),
        row("GITHUB_TOKEN", True, config.GITHUB_TOKEN, "Token fino de GitHub con Contents: read & write sobre los repos destino."),
        row("GITHUB_REPO", True, config.GITHUB_REPO, "Repositorio destino por defecto, formato owner/nombre."),
        row("AGENT_ALLOWED_REPOS", False, os.getenv("AGENT_ALLOWED_REPOS"), "Lista (coma) de repos que el selector puede usar. Por defecto solo GITHUB_REPO."),
        row("OLLAMA_BASE_URL", False, os.getenv("OLLAMA_BASE_URL") or os.getenv("AGENT_OLLAMA_URL"), "Solo si Ollama corre en otro servicio; por defecto http://127.0.0.1:11434 dentro del contenedor."),
        row("BASE_MODEL / EXTRA_MODELS", False, os.getenv("BASE_MODEL") or os.getenv("EXTRA_MODELS"), "Modelo base y modelos extra que se descargan al arrancar (p. ej. deepseek-coder-v2:16b)."),
        row("SEARXNG_URL / BRAVE_API_KEY", False, config.SEARXNG_URL or config.BRAVE_API_KEY, "Proveedor de búsqueda web (opcional). Sin ellos se usa DuckDuckGo en modo mejor esfuerzo; PyPI y npm siempre funcionan."),
        row("AGENT_WEB / AGENT_MEMORY", False, os.getenv("AGENT_WEB") or os.getenv("AGENT_MEMORY"), "0 desactiva la web o la memoria (por defecto activas)."),
        row("AGENT_TARGET_BRANCH", False, config.TARGET_BRANCH, "Vacío = rama nueva por tarea (recomendado). main = push directo."),
    ]


def _worker() -> None:
    while True:
        task = QUEUE.get()
        task["status"] = "running"
        try:
            result = engine.run_task(task, Workspace(task["repo"]))
            task["result"] = result
            task["error"] = result.get("error")
            task["status"] = result.get("status", "failed")
        except GitError as exc:
            task["status"], task["error"] = "failed", str(exc)
        except Exception:  # noqa: BLE001 - no tumbar el worker
            log.exception("error interno en la tarea %s", task["id"])
            task["status"], task["error"] = "failed", "error interno del agente (revisa los logs)"
        task["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        QUEUE.task_done()


def _validate_request(body: dict) -> tuple[dict | None, str | None]:
    instruction = body.get("instruction")
    if not isinstance(instruction, str) or not (10 <= len(instruction.strip()) <= config.MAX_INSTRUCTION):
        return None, f"'instruction' debe ser texto de 10 a {config.MAX_INSTRUCTION} caracteres"
    repo = body.get("repo") or config.GITHUB_REPO
    if repo not in config.ALLOWED_REPOS:
        return None, "repo no permitido (ver AGENT_ALLOWED_REPOS)"
    mode = body.get("mode") or "task"
    if mode not in ("task", "project"):
        return None, "'mode' debe ser 'task' o 'project'"
    model = body.get("model") or ""
    if model and model != config.MODEL:
        if not isinstance(model, str) or not MODEL_RE.match(model):
            return None, "'model' tiene caracteres no válidos"
        if model not in installed_models():
            return None, f"el modelo '{model}' no está instalado en Ollama (añádelo a EXTRA_MODELS o usa ollama pull)"
    lists = {}
    for key, limit in (("paths", config.MAX_FILES), ("context_files", 5)):
        value = body.get(key) or []
        if not isinstance(value, list) or len(value) > limit or not all(isinstance(x, str) and len(x) < 300 for x in value):
            return None, f"'{key}' debe ser una lista de hasta {limit} rutas"
        lists[key] = value
    return {
        "instruction": instruction.strip(), "repo": repo, "mode": mode, "model": model or config.MODEL,
        "dry_run": bool(body.get("dry_run", False)), **lists,
    }, None


class Handler(BaseHTTPRequestHandler):
    server_version = "agent/1.0"

    def log_message(self, fmt, *args):  # no registrar cabeceras ni tokens
        log.info("%s %s", self.address_string(), fmt % args)

    def _send_static(self, name: str, ctype: str) -> None:
        try:
            with open(os.path.join(UI_DIR, name), "rb") as fh:
                data = fh.read()
        except OSError:
            return self._send(404, {"error": "interfaz no disponible"})
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _send(self, code: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        expected = f"Bearer {config.API_TOKEN}".encode()
        given = self.headers.get("Authorization", "").encode()
        return bool(config.API_TOKEN) and hmac.compare_digest(given, expected)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/")
        if path in STATIC:  # el dashboard es público; todos los datos van por /agent/* con token
            return self._send_static(*STATIC[path])
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        if path == "/agent/health":
            return self._send(200, {
                "ok": True, "model": config.MODEL, "repo": config.GITHUB_REPO,
                "target_branch": config.TARGET_BRANCH or "agent/<fecha>-<slug>", "queued": QUEUE.qsize(),
            })
        if path == "/agent/models":
            installed = installed_models()
            return self._send(200, {"default": config.MODEL, "installed": installed, "suggested": config.SUGGESTED_MODELS})
        if path == "/agent/config":
            installed = installed_models()
            return self._send(200, {
                "model": config.MODEL, "models": {"installed": installed, "suggested": config.SUGGESTED_MODELS},
                "repo": config.GITHUB_REPO, "repos": config.ALLOWED_REPOS,
                "target_branch": config.TARGET_BRANCH, "ollama_ok": bool(installed),
                "limits": {"max_files": config.MAX_FILES, "project_max_files": config.PROJECT_MAX_FILES,
                           "max_instruction": config.MAX_INSTRUCTION, "iterations": config.MAX_ITERATIONS,
                           "llm_review": config.LLM_REVIEW},
                "env": _env_checklist(), "web": web.status(),
                "memory": {"enabled": config.MEMORY_ENABLED, "ttl_days": config.MEMORY_PROJECT_TTL_DAYS, "max_bytes": config.MEMORY_MAX_BYTES},
            })
        if path == "/agent/memory":
            return self._send(200, memory.manager().stats())
        if path == "/agent/tasks":
            with TASKS_LOCK:
                items = [{"id": t["id"], "status": t["status"], "created": t["created"], "mode": t.get("mode"),
                          "instruction": t.get("instruction", "")[:80]} for t in reversed(TASKS.values())]
            return self._send(200, {"tasks": items})
        if path.startswith("/agent/task/"):
            with TASKS_LOCK:
                task = TASKS.get(path.rsplit("/", 1)[1])
            return self._send(200, _public(task)) if task else self._send(404, {"error": "tarea no encontrada"})
        self._send(404, {"error": "no encontrado"})

    def _memory_purge(self, body: dict) -> None:
        mem = memory.manager()
        if body.get("all") is True:
            result = {"forgotten_projects": mem.forget(None)}
        elif "repo" in body:
            if body["repo"] not in config.ALLOWED_REPOS:
                return self._send(400, {"error": "repo no permitido (ver AGENT_ALLOWED_REPOS)"})
            result = {"forgotten_projects": mem.forget(body["repo"])}
        else:
            result = mem.purge()
        self._send(200, {"result": result, "memory": mem.stats()})

    def do_POST(self):  # noqa: N802
        if not self._authorized():
            return self._send(401, {"error": "unauthorized"})
        route = self.path.split("?", 1)[0].rstrip("/")
        if route not in ("/agent/task", "/agent/memory/purge"):
            return self._send(404, {"error": "no encontrado"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if not 0 < length <= MAX_BODY:
            return self._send(413, {"error": f"cuerpo requerido de hasta {MAX_BODY} bytes"})
        try:
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._send(400, {"error": "JSON inválido"})
        if route == "/agent/memory/purge":
            return self._memory_purge(body)
        data, err = _validate_request(body)
        if err:
            return self._send(400, {"error": err})
        task = {
            "id": uuid.uuid4().hex[:12], "status": "queued", "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "steps": [], "result": None, "error": None, **data,
        }
        with TASKS_LOCK:
            TASKS[task["id"]] = task
            while len(TASKS) > MAX_TASKS:
                TASKS.popitem(last=False)
        QUEUE.put(task)
        self._send(202, {"id": task["id"], "status": "queued", "poll": f"/agent/task/{task['id']}"})


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not config.API_TOKEN:
        sys.exit("Falta API_TOKEN")
    if not config.GITHUB_TOKEN or not config.GITHUB_REPO:
        sys.exit("Faltan GITHUB_TOKEN y/o GITHUB_REPO")
    try:
        purged = memory.manager().purge()  # arranque limpio: descarta lo caducado
        log.info("Memoria: purga inicial %s", purged)
    except OSError as exc:
        log.warning("Memoria: no se pudo purgar al arrancar (%s)", exc)
    threading.Thread(target=_worker, daemon=True).start()
    srv = ThreadingHTTPServer((config.HOST, config.PORT), Handler)
    log.info("Agente escuchando en %s:%s (repo %s)", config.HOST, config.PORT, config.GITHUB_REPO)
    srv.serve_forever()


if __name__ == "__main__":
    main()
