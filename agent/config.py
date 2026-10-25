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
