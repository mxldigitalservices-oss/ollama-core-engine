"""Configuración del agente programador. Todo se controla con variables de entorno."""
from __future__ import annotations

import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _list(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if raw is None:
        return list(default)
    return [x.strip() for x in raw.split(",") if x.strip()]


# --- Servicio local (solo 127.0.0.1; Caddy lo expone bajo /agent/*) ---
HOST = "127.0.0.1"
PORT = _int("AGENT_PORT", 8081)
API_TOKEN = os.getenv("API_TOKEN", "")

# --- Ollama ---
# OLLAMA_BASE_URL es el alias habitual; solo hace falta si Ollama corre en otro servicio.
OLLAMA_URL = (os.getenv("OLLAMA_BASE_URL") or os.getenv("AGENT_OLLAMA_URL") or "http://127.0.0.1:11434").rstrip("/")
MODEL = os.getenv("AGENT_MODEL", "elite-architect")
# Modelos sugeridos en el selector de la interfaz (además de los instalados en Ollama).
SUGGESTED_MODELS = _list("AGENT_SUGGESTED_MODELS", ["elite-architect", "qwen2.5-coder:7b", "qwen2.5-coder:14b", "deepseek-coder-v2:16b"])
NUM_CTX = _int("AGENT_NUM_CTX", 16384)
NUM_PREDICT = _int("AGENT_NUM_PREDICT", 8192)
LLM_TIMEOUT = _int("AGENT_LLM_TIMEOUT", 900)

# --- GitHub ---
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")  # formato owner/nombre
ALLOWED_REPOS = _list("AGENT_ALLOWED_REPOS", [GITHUB_REPO] if GITHUB_REPO else [])
REMOTE_TEMPLATE = os.getenv("AGENT_REMOTE_TEMPLATE", "https://github.com/{repo}.git")
BASE_BRANCH = os.getenv("AGENT_BASE_BRANCH", "")  # vacío = rama por defecto del remoto
# Vacío = cada tarea sube a una rama nueva agent/<fecha>-<slug> (recomendado).
# Con un valor (por ejemplo "main") empuja directo a esa rama.
TARGET_BRANCH = os.getenv("AGENT_TARGET_BRANCH", "")
GIT_NAME = os.getenv("AGENT_GIT_NAME", "ollama-core-engine-agent")
GIT_EMAIL = os.getenv("AGENT_GIT_EMAIL", "agent@users.noreply.github.com")
GIT_TIMEOUT = _int("AGENT_GIT_TIMEOUT", 120)
WORKSPACE = os.getenv("AGENT_WORKSPACE", "/root/.ollama/agent-workspace")

# --- Búsqueda web autónoma (documentación, dependencias, referencias técnicas) ---
WEB_ENABLED = os.getenv("AGENT_WEB", "1") != "0"
# auto = SearXNG si hay SEARXNG_URL, Brave si hay BRAVE_API_KEY, si no DuckDuckGo (mejor esfuerzo). none = sin búsqueda.
SEARCH_PROVIDER = os.getenv("AGENT_SEARCH_PROVIDER", "auto").lower()
SEARXNG_URL = os.getenv("SEARXNG_URL", "").rstrip("/")
BRAVE_API_KEY = os.getenv("BRAVE_API_KEY", "")
WEB_TIMEOUT = _int("AGENT_WEB_TIMEOUT", 8)
WEB_MAX_SEARCHES = _int("AGENT_WEB_MAX_SEARCHES", 3)   # por tarea
WEB_MAX_FETCHES = _int("AGENT_WEB_MAX_FETCHES", 2)     # páginas de documentación por tarea
WEB_MAX_PACKAGE_LOOKUPS = _int("AGENT_WEB_MAX_PACKAGES", 60)
WEB_CACHE_TTL = _int("AGENT_WEB_CACHE_TTL", 86_400)    # segundos
# Páginas que el agente puede DESCARGAR (los resultados de búsqueda solo aportan título y extracto).
WEB_ALLOWED_DOMAINS = _list("AGENT_WEB_ALLOWED_DOMAINS", [
    "docs.python.org", "docs.pydantic.dev", "fastapi.tiangolo.com", "flask.palletsprojects.com",
    "developer.mozilla.org", "react.dev", "expressjs.com", "nodejs.org", "tailwindcss.com", "www.postgresql.org",
    "redis.io", "www.mongodb.com", "dev.mysql.com", "docs.docker.com", "caddyserver.com", "docs.railway.com",
    "render.com", "readthedocs.io", "docs.sqlalchemy.org", "www.sqlalchemy.org", "requests.readthedocs.io",
])
# Un paquete que el modelo agrega debe existir y tener historial; si no, se rechaza (anti paquete inventado).
PACKAGE_MIN_RELEASES = _int("AGENT_PACKAGE_MIN_RELEASES", 3)
PACKAGE_MIN_AGE_DAYS = _int("AGENT_PACKAGE_MIN_AGE_DAYS", 30)

# --- Memoria ligera por proyecto (carpetas locales de conocimiento) ---
MEMORY_ENABLED = os.getenv("AGENT_MEMORY", "1") != "0"
KNOWLEDGE_DIR = os.getenv("AGENT_KNOWLEDGE_DIR") or os.path.join(os.path.dirname(WORKSPACE.rstrip("/")) or "/", "agent-knowledge")
MEMORY_MAX_BYTES = _int("AGENT_MEMORY_MAX_BYTES", 5_000_000)
MEMORY_PROJECT_TTL_DAYS = _int("AGENT_MEMORY_TTL_DAYS", 30)   # proyectos inactivos se descartan
MEMORY_MAX_LESSONS = _int("AGENT_MEMORY_MAX_LESSONS", 40)
MEMORY_MAX_FILES = _int("AGENT_MEMORY_MAX_FILES", 200)
MEMORY_PROMPT_CHARS = _int("AGENT_MEMORY_PROMPT_CHARS", 2500)

# --- Ciclo de autoanálisis ---
MAX_ITERATIONS = max(1, _int("AGENT_MAX_ITERATIONS", 3))
LLM_REVIEW = os.getenv("AGENT_LLM_REVIEW", "1") != "0"

# --- Límites y barreras ---
MAX_FILES = _int("AGENT_MAX_FILES", 10)
# Modo proyecto (plan -> generación por lotes -> validación -> empaquetado)
PROJECT_MAX_FILES = _int("AGENT_PROJECT_MAX_FILES", 30)
PROJECT_BATCH = max(1, _int("AGENT_PROJECT_BATCH", 4))
MAX_FILE_BYTES = _int("AGENT_MAX_FILE_BYTES", 200_000)
MAX_INSTRUCTION = 4000
MAX_CONTEXT_CHARS = _int("AGENT_MAX_CONTEXT_CHARS", 20000)

# Repos "propios" (el motor mismo): en ellos rigen todas las PROTECTED_PATHS.
# En los demás repos (proyectos de clientes) solo se protegen .git/, .github/ y .env*.
PROTECTED_REPOS = _list("AGENT_PROTECTED_REPOS", [GITHUB_REPO] if GITHUB_REPO else [])
ALWAYS_PROTECTED = [".git/", ".github/"]

# Rutas que el agente nunca puede escribir (prefijo con "/" final = carpeta).
PROTECTED_PATHS = _list(
    "AGENT_PROTECTED_PATHS",
    [
        ".git/",
        ".github/",
        "agent/",
        "config/Caddyfile",
        "docker/Dockerfile",
        "scripts/start.sh",
        "railway.json",
    ],
)

ALLOWED_EXT = {
    ".py", ".json", ".sh", ".md", ".txt", ".yml", ".yaml", ".toml", ".ini", ".cfg",
    ".html", ".css", ".js", ".sql", ".xml", ".xsd", ".xsl", ".xslt",
    # Frontend / full-stack
    ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".ejs", ".svg", ".webmanifest", ".conf",
}
# Archivos sin extensión (o con nombre especial) permitidos por nombre exacto.
ALLOWED_NAMES = {
    "dockerfile", "caddyfile", "procfile", "makefile", ".dockerignore", ".gitignore",
    ".gitattributes", ".env.example", ".env.sample", ".editorconfig",
}
