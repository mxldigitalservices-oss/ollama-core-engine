"""Ciclo del agente: generar -> validar -> revisar -> corregir -> commit/push."""
from __future__ import annotations

import functools
import logging
import re
import time
from dataclasses import dataclass, field

from . import config, memory, validators
from . import research as research_mod
from .autogit import publish
from .gitops import GitError, Workspace
from .llm import LLMError, chat

log = logging.getLogger("agent")

EXPERTISE = """ESPECIALIDADES (desarrolladora senior full-stack y arquitecta de software):
- Frontend: HTML5 semántico y accesible, CSS3, JavaScript/TypeScript moderno, React (componentes funcionales y hooks), plantillas EJS, TailwindCSS y PWAs (manifest, service worker, modo offline).
- Backend y APIs: Python (FastAPI, Flask), Node.js (Express), APIs REST con validación, códigos HTTP correctos, autenticación y manejo de errores.
- Bases de datos: modelado y consultas para PostgreSQL, MySQL, MongoDB y Redis (índices, migraciones, consultas parametrizadas, caché).
- DevOps: Docker (imágenes ligeras, multi-stage), Caddy, scripts Bash/Linux y despliegues en Railway y Render (PORT, healthchecks, variables de entorno).
- Automatización: web scraping respetuoso, procesamiento de archivos, XML (XSD, XPath, XSLT, XMLDSig), PDF y lógica compleja."""

SYSTEM_AGENT = """Eres ELITE-ARCHITECT en MODO AGENTE PROGRAMADOR. Recibes una instrucción y devuelves archivos completos listos para commit.

""" + EXPERTISE + """

PASO 1 - RAZONA (obligatorio, cadena de pensamiento). Escribe un bloque <analisis>...</analisis>, breve, con:
a) requisito, entradas/salidas y archivos afectados; b) diseño: pasos, estructuras y arquitectura; c) errores probables y casos borde; d) lista de cada import con su origen (biblioteca estándar, repositorio o requirements.txt / package.json); e) repaso de sintaxis, indentación y nombres definidos.
PASO 2 - RESPONDE después del bloque, con este formato estricto y sin más texto:
=== COMMIT ===
tipo: resumen en imperativo, máximo 72 caracteres (tipo = feat, fix, docs, refactor, test o chore)

Cuerpo opcional explicando el porqué.
=== FILE: ruta/relativa/archivo.py ===
contenido completo del archivo
=== END FILE ===

REGLAS:
1. Rutas relativas a la raíz del repositorio, sin '..' ni rutas absolutas.
2. Cada archivo va completo: nunca fragmentos ni "resto del código".
3. Todo import debe resolverse: biblioteca estándar, módulos del repositorio o paquetes declarados en requirements.txt (Python) o package.json (Node). Si agregas una dependencia, incluye el manifiesto actualizado.
4. Nunca incluyas secretos, tokens ni URLs con credenciales. Las conexiones a PostgreSQL, MySQL, Redis y MongoDB se leen de variables de entorno (DATABASE_URL, MYSQL_URL, REDIS_URL, MONGO_URL). Servidores web: escuchan en 0.0.0.0 y el puerto de la variable PORT.
5. Consultas SQL parametrizadas, entradas validadas y salida HTML escapada.
6. No uses bloques ``` dentro de los archivos ni de la respuesta.
7. Máximo {max_files} archivos."""

REVIEW_SYSTEM = (
    "Eres un revisor de código estricto. Verificas que el código cumpla la instrucción y buscas "
    "errores de lógica, imports faltantes, manejo de errores y fallos de seguridad. Responde SOLO así: "
    "la primera línea es 'APROBADO' o 'RECHAZADO'. Si rechazas, añade hasta 5 líneas con el formato "
    "'- archivo:línea: problema concreto y verificable'. No reportes estilo ni suposiciones."
)

_FILE_RE = re.compile(
    r"^=== FILE: (?P<path>[^\n]+?) ===[ \t]*\n(?P<body>.*?)\n=== END FILE ===[ \t]*$", re.S | re.M
)
_COMMIT_RE = re.compile(r"^=== COMMIT ===[ \t]*\n(?P<msg>.*?)(?=^=== FILE: |\Z)", re.S | re.M)


@dataclass
class Parsed:
    files: dict[str, str] = field(default_factory=dict)
    commit: str = ""


_REASONING_RE = re.compile(r"<(think|analisis)>.*?</\1>", re.S | re.I)


def strip_reasoning(text: str) -> str:
    """Quita bloques <think>/<analisis> (el razonamiento no debe interpretarse como archivos)."""
    return _REASONING_RE.sub("", text)


def extract_reasoning(text: str, limit: int = 6000) -> str:
    """Devuelve el razonamiento (<analisis>/<think>) para mostrarlo en la interfaz."""
    found = [m.group(0) for m in _REASONING_RE.finditer(text)]
    clean = "\n".join(re.sub(r"</?(think|analisis)>", "", f, flags=re.I).strip() for f in found)
    return clean[:limit]


def make_step(task: dict):
    steps = task.setdefault("steps", [])

    def step(msg: str) -> None:
        steps.append({"t": time.strftime("%H:%M:%S"), "msg": msg})
        log.info("[%s] %s", task.get("id", "-"), msg)

    return step


def record_validation(task: dict, stage: str, report, *, extra_errors: list[str] | None = None, notes: list[str] | None = None) -> None:
    errors = ([str(i) for i in report.errors] if report else []) + (extra_errors or [])
    task.setdefault("validation", []).append({
        "stage": stage, "ok": not errors and not notes, "errors": errors[:20],
        "warnings": [str(w) for w in (report.warnings if report else [])][:20],
        "tools": list(report.tools) if report else [], "review": notes or [],
    })


def render(parsed: "Parsed") -> str:
    """Reconstruye la respuesta sin el razonamiento (ahorra contexto en las iteraciones)."""
    parts = []
    if parsed.commit:
        parts.append("=== COMMIT ===\n" + parsed.commit)
    for path, content in parsed.files.items():
        parts.append(f"=== FILE: {path} ===\n{content.rstrip(chr(10))}\n=== END FILE ===")
    return "\n".join(parts)


def _strip_fences(body: str) -> str:
    lines = body.split("\n")
    if lines and lines[0].lstrip().startswith("```"):
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
    return "\n".join(lines)


def parse_response(text: str) -> Parsed:
    parsed = Parsed()
    text = strip_reasoning(text)
    for m in _FILE_RE.finditer(text):
        parsed.files[m.group("path").strip()] = _strip_fences(m.group("body")).rstrip("\n") + "\n"
    m = _COMMIT_RE.search(text)
    if m:
        parsed.commit = m.group("msg").strip()
    return parsed


def parse_review(text: str) -> list[str]:
    text = strip_reasoning(text)
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if not lines or not lines[0].lstrip("*# ").upper().startswith("RECHAZADO"):
        return []
    return [ln.lstrip("-* ").strip() for ln in lines[1:] if ln.startswith(("-", "*"))][:5]


def _numbered(files: dict[str, str], budget: int) -> str:
    parts: list[str] = []
    for rel, content in files.items():
        body = "\n".join(f"{i}: {ln}" for i, ln in enumerate(content.splitlines(), 1))
        parts.append(f"### {rel}\n{body}")
    text = "\n\n".join(parts)
    return text[:budget] + ("\n[... truncado ...]" if len(text) > budget else "")


def _first_prompt(task: dict, ws: Workspace, with_context: bool, memory_ctx: str = "") -> str:
    parts = [f"INSTRUCCIÓN:\n{task['instruction']}"]
    if memory_ctx:
        parts.append(memory_ctx)
    if task.get("paths"):
        parts.append("Archivos a crear o modificar: " + ", ".join(task["paths"]))
    if with_context:
        tree = ws.tree()
        if tree:
            parts.append("ARCHIVOS DEL REPOSITORIO (muestra):\n" + "\n".join(tree))
        budget = config.MAX_CONTEXT_CHARS
        ctx: list[str] = []
        for rel in task.get("context_files") or []:
            text = ws.read(rel, max(budget, 0))
            if text is None:
                continue
            ctx.append(f"### {rel}\n{text}")
            budget -= len(text)
            if budget <= 0:
                break
        if ctx:
            parts.append("ARCHIVOS EXISTENTES RELEVANTES:\n" + "\n\n".join(ctx))
    return "\n\n".join(parts)


def review(llm, instruction: str, files: dict[str, str]) -> list[str]:
    prompt = f"INSTRUCCIÓN:\n{instruction}\n\nCÓDIGO:\n{_numbered(files, config.MAX_CONTEXT_CHARS)}"
    messages = [{"role": "system", "content": REVIEW_SYSTEM}, {"role": "user", "content": prompt}]
    return parse_review(llm(messages, temperature=0.0))


def run_task(task: dict, ws: Workspace, llm=chat) -> dict:
    """Ejecuta una tarea completa. Devuelve un dict con 'status' = done | failed | no_changes."""
    model = task.get("model") or config.MODEL
    if llm is chat and task.get("model"):
        llm = functools.partial(chat, model=task["model"])
    step = make_step(task)
    if task.get("mode") == "project":
        from . import project  # import diferido: project usa utilidades de este módulo
        return project.run_project(task, ws, llm, step, model)

    secrets = (config.API_TOKEN, config.GITHUB_TOKEN)
    try:
        step(f"sincronizando repositorio ({ws.repo}) - modelo {model}")
        ws.sync()
        research = research_mod.for_task(task, step)
        mem_ctx = recall(task, ws, step)
        system = {"role": "system", "content": research_mod.system_with_search(SYSTEM_AGENT.format(max_files=config.MAX_FILES), research)}
        messages = [system, {"role": "user", "content": _first_prompt(task, ws, True, mem_ctx)}]
        short_prompt = _first_prompt(task, ws, False, mem_ctx)

        candidate: dict | None = None
        last_errors: list[str] = []
        iterations = 0
        for it in range(1, config.MAX_ITERATIONS + 1):
            iterations = it
            step(f"iteración {it}/{config.MAX_ITERATIONS}: generando código")
            raw = research_mod.generate(llm, messages, research, step)
            thoughts = extract_reasoning(raw)
            if thoughts:
                task.setdefault("reasoning", []).append({"stage": f"Iteración {it}", "text": thoughts})
            parsed = parse_response(raw)
            if not parsed.files:
                last_errors = ["formato inválido: usa '=== FILE: ruta ===' ... '=== END FILE ===' con el contenido completo"]
                report = None
            else:
                report = research_mod.validate(parsed.files, research, step, ws.root, lambda f: validators.validate_files(
                    f, ws.root, secret_values=secrets, repo=ws.repo))
                last_errors = [str(i) for i in report.errors]

            if last_errors:
                step(f"validación estática: {len(last_errors)} error(es)")
                record_validation(task, f"Iteración {it}: validación estática", report, extra_errors=None if report else last_errors)
                feedback = last_errors
            else:
                step(f"validación estática correcta ({len(report.warnings)} advertencia(s))")
                notes: list[str] = []
                if config.LLM_REVIEW:
                    step("revisión lógica (LLM)")
                    try:
                        notes = review(llm, task["instruction"], report.files)
                    except LLMError as exc:
                        notes = []
                        step(f"revisión LLM no disponible: {exc}")
                record_validation(task, f"Iteración {it}: estática + revisión", report, notes=notes)
                candidate = {"parsed": parsed, "report": report, "notes": notes}
                if not notes:
                    break
                step(f"el revisor reportó {len(notes)} observación(es)")
                feedback = [f"[revisión] {n}" for n in notes]

            if it == config.MAX_ITERATIONS:
                break
            task.setdefault("_errors", []).extend(f for f in feedback if not f.startswith("[revisión]"))
            fb = "Corrige TODOS estos problemas y devuelve los archivos completos en el mismo formato:\n" + "\n".join(f"- {f}" for f in feedback)
            extra = research_mod.web_feedback(research, feedback, step)
            if extra:
                fb += "\n\n" + extra
            messages = [system, {"role": "user", "content": short_prompt},
                        {"role": "assistant", "content": render(parsed) if parsed.files else strip_reasoning(raw)[:4000]}, {"role": "user", "content": fb}]

        if candidate is None:
            return {"status": "failed", "iterations": iterations, "model": model,
                    "error": "No se obtuvo código que pase la validación estática", "errors": last_errors}
        return finish(task, ws, candidate["report"], candidate["parsed"].commit, candidate["notes"], iterations, model, step)
    except (LLMError, GitError) as exc:
        step(f"error: {exc}")
        try:
            ws.reset()
        except Exception:  # noqa: BLE001 - limpieza best-effort
            pass
        return {"status": "failed", "error": str(exc)}


def recall(task: dict, ws: Workspace, step) -> str:
    """Contexto compacto del proyecto guardado en la memoria (vacío si no hay o está desactivada)."""
    try:
        mem = memory.for_repo(ws.repo)
        if not mem:
            return ""
        text = mem.prompt(task["instruction"])
    except (OSError, ValueError) as exc:  # la memoria nunca debe bloquear una tarea
        step(f"memoria no disponible: {exc}")
        return ""
    task["memory"] = {"used": bool(text), "lessons": len(mem.lessons()) if text else 0}
    if text:
        step("memoria: se reutiliza el contexto guardado del proyecto")
    return text


def remember(task: dict, ws: Workspace, report, step) -> None:
    """Guarda lo esencial del proyecto (mapa de archivos, lecciones) y purga lo caducado."""
    try:
        mem = memory.for_repo(ws.repo)
        if not mem:
            return
        mem.record(task, report.files, ws.root, task.get("_errors"), task.get("plan"))
        purged = memory.manager().purge()
        task["memory"] = {**task.get("memory", {}), "saved": True, "purged": sum(purged.values())}
        step("memoria actualizada" + (f" (se descartaron {sum(purged.values())} elemento(s) antiguos)" if sum(purged.values()) else ""))
    except (OSError, ValueError) as exc:
        step(f"memoria no actualizada: {exc}")


def finish(task: dict, ws: Workspace, report, commit_raw: str, notes: list[str], iterations: int, model: str, step) -> dict:
    """Último tramo común: arma el resultado y, salvo dry_run, hace add/commit/push (autogit)."""
    result = {
        "iterations": iterations, "model": model,
        "files": [{"path": p, "bytes": len(c.encode("utf-8"))} for p, c in report.files.items()],
        "warnings": [str(w) for w in report.warnings],
        "review_notes": notes,
        "tools": report.tools + (["llm-review"] if config.LLM_REVIEW else []),
    }
    if task.get("dry_run"):
        step("dry_run: no se escribe ni se sube nada")
        result.update(status="done", dry_run=True, contents=report.files)
        return result
    step("autogit: add / commit / push")
    info = publish(ws, report, commit_raw, model=model)
    if info is None:
        result["status"] = "no_changes"
        return result
    result.update(info)
    result["status"] = "done"
    remember(task, ws, report, step)
    step(f"listo: {info['commit'][:8]} en {info['branch']}" + (" (push verificado)" if info.get("verified") else ""))
    return result
