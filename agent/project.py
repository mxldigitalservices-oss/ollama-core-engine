"""Modo proyecto: de un requerimiento abierto a un proyecto completo y publicado.

Flujo: planificar (arquitectura + lista de archivos) -> generar por lotes -> empaquetar
(README, .gitignore, .env.example) -> validar -> revisar -> corregir -> autogit (commit + push).
"""
from __future__ import annotations

import json
import posixpath
import re

from . import config, engine, validators
from .gitops import GitError
from .llm import LLMError

SYSTEM_PLAN = "Eres ELITE-ARCHITECT en MODO ARQUITECTO DE PROYECTOS. Diseñas la solución completa antes de escribirla.\n\n" + engine.EXPERTISE + """

PASO 1 - RAZONA (obligatorio, cadena de pensamiento) en un bloque <analisis>...</analisis>: objetivo y usuarios, stack elegido y por qué, modelo de datos, endpoints/pantallas, estructura de carpetas, variables de entorno, riesgos (seguridad, errores, despliegue en Railway/Render).
PASO 2 - Devuelve el plan, sin más texto, entre estas marcas y con JSON válido:
=== PLAN ===
{"name": "slug-del-proyecto", "summary": "qué hace, en 1-2 frases", "stack": ["Python", "FastAPI", "PostgreSQL"],
 "commit": "feat: crea <proyecto>", "env": ["DATABASE_URL"], "run": ["comando para ejecutarlo localmente"],
 "files": [{"path": "backend/main.py", "purpose": "qué contiene"}]}
=== END PLAN ===

REGLAS DEL PLAN:
1. Entre 1 y {max_files} archivos, en orden de dependencia (manifiestos y modelos primero, vistas y README después).
2. Incluye lo necesario para que funcione de punta a punta: backend, frontend, esquema de base de datos (SQL o modelos), manifiesto de dependencias (requirements.txt / package.json), Dockerfile y README.md cuando apliquen.
3. Rutas relativas, sin '..'. Extensiones permitidas: py, js, jsx, ts, tsx, mjs, cjs, ejs, html, css, svg, json, webmanifest, sql, sh, md, txt, yml, yaml, toml, xml, conf, y los nombres Dockerfile, Caddyfile, Procfile, .gitignore, .dockerignore.
4. No incluyas archivos .env (usa la lista "env"), ni binarios, ni imágenes.
5. Servidores en 0.0.0.0 con el puerto de PORT; bases de datos solo por variables de entorno."""

GITIGNORE = """__pycache__/
*.pyc
.venv/
venv/
node_modules/
dist/
build/
.env
.env.*
!.env.example
*.log
.DS_Store
"""

_PLAN_RE = re.compile(r"=== PLAN ===[ \t]*\n(?P<body>.*?)\n=== END PLAN ===", re.S)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "proyecto"


def parse_plan(raw: str, protected: list[str]) -> tuple[dict | None, list[str]]:
    """Convierte la respuesta del modelo en un plan válido o devuelve la lista de problemas."""
    m = _PLAN_RE.search(engine.strip_reasoning(raw))
    if not m:
        return None, ["falta el bloque '=== PLAN === ... === END PLAN ==='"]
    body = re.sub(r"^```\w*\n|\n```$", "", m.group("body").strip())
    try:
        data = json.loads(body)
    except ValueError as exc:
        return None, [f"el plan no es JSON válido: {exc}"]
    if not isinstance(data, dict) or not isinstance(data.get("files"), list) or not data["files"]:
        return None, ["el plan debe incluir 'files' con al menos un archivo"]
    problems: list[str] = []
    files: list[dict] = []
    seen: set[str] = set()
    for item in data["files"]:
        raw_path = item.get("path") if isinstance(item, dict) else item
        if not isinstance(raw_path, str):
            problems.append("cada archivo del plan necesita 'path' de tipo texto")
            continue
        norm, issue = validators.check_path(raw_path, protected, config.ALLOWED_EXT, config.ALLOWED_NAMES)
        if issue:
            problems.append(str(issue))
        elif norm not in seen:
            seen.add(norm)
            purpose = item.get("purpose", "") if isinstance(item, dict) else ""
            files.append({"path": norm, "purpose": str(purpose)[:200]})
    if len(files) > config.PROJECT_MAX_FILES:
        problems.append(f"el plan supera el máximo de {config.PROJECT_MAX_FILES} archivos")
    if problems:
        return None, problems
    def strings(key: str, limit: int) -> list[str]:
        value = data.get(key)
        return [str(x)[:200] for x in value][:limit] if isinstance(value, list) else []
    env = [e for e in strings("env", 30) if re.fullmatch(r"[A-Z][A-Z0-9_]*", e)]
    return {
        "name": _slug(str(data.get("name", ""))), "summary": str(data.get("summary", ""))[:400],
        "stack": strings("stack", 12), "env": env, "run": strings("run", 6),
        "commit": str(data.get("commit", ""))[:200], "files": files,
    }, []


def package(files: dict[str, str], plan: dict) -> list[str]:
    """Empaquetado: agrega lo que falte para que el repo sea usable. Devuelve lo agregado."""
    added: list[str] = []
    if ".gitignore" not in files:
        files[".gitignore"] = GITIGNORE
        added.append(".gitignore")
    if plan["env"] and ".env.example" not in files:
        files[".env.example"] = "".join(f"{name}=\n" for name in plan["env"])
        added.append(".env.example")
    if "README.md" not in files:
        lines = [f"# {plan['name']}", "", plan["summary"], ""]
        if plan["stack"]:
            lines += ["## Stack", "", ", ".join(plan["stack"]), ""]
        lines += ["## Estructura", "", "| Archivo | Contenido |", "|---|---|"]
        lines += [f"| `{f['path']}` | {f['purpose']} |" for f in plan["files"]]
        if plan["env"]:
            lines += ["", "## Variables de entorno", "", "Copia `.env.example` y completa los valores:", ""]
            lines += [f"- `{name}`" for name in plan["env"]]
        if plan["run"]:
            lines += ["", "## Ejecutar", "", *[f"    {cmd}" for cmd in plan["run"]]]
        lines += ["", "## Despliegue", "", "Listo para Railway o Render: el servidor lee `PORT` y los secretos salen del entorno.", "",
                  "_Generado por ollama-core-engine._", ""]
        files["README.md"] = "\n".join(lines)
        added.append("README.md")
    return added


def _plan_digest(plan: dict) -> str:
    listing = "\n".join(f"- {f['path']}: {f['purpose']}" for f in plan["files"])
    return (f"PROYECTO: {plan['name']}\nRESUMEN: {plan['summary']}\nSTACK: {', '.join(plan['stack'])}\n"
            f"VARIABLES DE ENTORNO: {', '.join(plan['env']) or '(ninguna)'}\nARCHIVOS PLANIFICADOS:\n{listing}")


def _context(files: dict[str, str], budget: int) -> str:
    parts: list[str] = []
    for rel, content in files.items():
        chunk = f"### {rel}\n{content[:max(budget, 0)]}"
        parts.append(chunk)
        budget -= len(chunk)
        if budget <= 0:
            break
    return "\n\n".join(parts)


def _ask_files(llm, system: dict, prompt: str, wanted: list[str], task: dict, stage: str) -> dict[str, str]:
    """Pide archivos concretos al modelo; solo acepta los solicitados."""
    raw = llm([system, {"role": "user", "content": prompt}])
    thoughts = engine.extract_reasoning(raw)
    if thoughts:
        task.setdefault("reasoning", []).append({"stage": stage, "text": thoughts})
    got: dict[str, str] = {}
    for rel, content in engine.parse_response(raw).files.items():
        norm = posixpath.normpath(rel.strip().strip("`'\""))
        if norm in wanted:
            got[norm] = content
    return got


def _attributable(issues: list[str], files: dict[str, str]) -> list[str]:
    """Archivos mencionados en errores o notas del revisor (formato '[tipo] ruta:línea: ...' o 'ruta:línea: ...')."""
    hit = [p for p in files if any(p in issue for issue in issues)]
    return hit


def run_project(task: dict, ws, llm, step, model: str) -> dict:
    protected = config.PROTECTED_PATHS if ws.repo in config.PROTECTED_REPOS else config.ALWAYS_PROTECTED
    secrets = (config.API_TOKEN, config.GITHUB_TOKEN)
    try:
        step(f"sincronizando repositorio ({ws.repo}) - modelo {model}")
        ws.sync()
        if ws.empty:
            step("repositorio vacío: el primer commit creará la rama principal")

        # 1) Plan ---------------------------------------------------------
        system_plan = {"role": "system", "content": SYSTEM_PLAN.replace("{max_files}", str(config.PROJECT_MAX_FILES))}
        request = f"REQUERIMIENTO:\n{task['instruction']}"
        plan, problems = None, []
        for attempt in (1, 2):
            step(f"planificando arquitectura (intento {attempt}/2)")
            prompt = request if not problems else request + "\n\nCORRIGE EL PLAN ANTERIOR:\n" + "\n".join(f"- {p}" for p in problems)
            raw = llm([system_plan, {"role": "user", "content": prompt}])
            thoughts = engine.extract_reasoning(raw)
            if thoughts:
                task.setdefault("reasoning", []).append({"stage": f"Plan (intento {attempt})", "text": thoughts})
            plan, problems = parse_plan(raw, protected)
            if plan:
                break
        if not plan:
            return {"status": "failed", "model": model, "error": "No se obtuvo un plan válido", "errors": problems}
        task["plan"] = {k: plan[k] for k in ("name", "summary", "stack", "env", "files")}
        step(f"plan: {len(plan['files'])} archivos ({', '.join(plan['stack']) or 'sin stack declarado'})")

        # 2) Generación por lotes ------------------------------------------
        system_gen = {"role": "system", "content": engine.SYSTEM_AGENT.format(max_files=config.PROJECT_BATCH)
                      + "\nEstás generando una parte de un proyecto mayor: devuelve SOLO los archivos pedidos, coherentes con el plan y con los ya generados (mismos nombres de funciones, rutas y variables)."}
        paths = [f["path"] for f in plan["files"]]
        files: dict[str, str] = {}
        batches = [paths[i:i + config.PROJECT_BATCH] for i in range(0, len(paths), config.PROJECT_BATCH)]
        for n, batch in enumerate(batches, 1):
            step(f"generando lote {n}/{len(batches)}: {', '.join(batch)}")
            wanted = list(batch)
            for attempt in (1, 2):
                prompt = (f"{_plan_digest(plan)}\n\nREQUERIMIENTO ORIGINAL:\n{task['instruction']}\n\n"
                          + (f"ARCHIVOS YA GENERADOS:\n{_context(files, config.MAX_CONTEXT_CHARS // 2)}\n\n" if files else "")
                          + "GENERA AHORA, completos y en el formato indicado, SOLO estos archivos: " + ", ".join(wanted))
                got = _ask_files(llm, system_gen, prompt, wanted, task, f"Lote {n}/{len(batches)}")
                files.update(got)
                wanted = [p for p in wanted if p not in got]
                if not wanted:
                    break
                step(f"faltan {len(wanted)} archivo(s) del lote; reintentando")
            if wanted:
                return {"status": "failed", "model": model, "error": f"El modelo no generó: {', '.join(wanted)}"}

        # 3) Empaquetar, validar, revisar y corregir ------------------------
        candidate: tuple | None = None
        last_errors: list[str] = []
        for it in range(1, config.MAX_ITERATIONS + 1):
            added = package(files, plan)
            if added:
                step("empaquetado: se agregó " + ", ".join(added))
            report = validators.validate_files(files, ws.root, secret_values=secrets, repo=ws.repo,
                                               max_files=config.PROJECT_MAX_FILES + 3)
            last_errors = [str(i) for i in report.errors]
            feedback: list[str]
            if last_errors:
                step(f"validación estática: {len(last_errors)} error(es)")
                engine.record_validation(task, f"Iteración {it}: validación estática", report)
                feedback = last_errors
            else:
                step(f"validación estática correcta ({len(report.warnings)} advertencia(s))")
                notes: list[str] = []
                if config.LLM_REVIEW:
                    step("revisión lógica (LLM)")
                    try:
                        notes = engine.review(llm, task["instruction"], report.files)
                    except LLMError as exc:
                        step(f"revisión LLM no disponible: {exc}")
                engine.record_validation(task, f"Iteración {it}: estática + revisión", report, notes=notes)
                candidate = (report, notes, it)
                if not notes:
                    break
                step(f"el revisor reportó {len(notes)} observación(es)")
                feedback = [f"[revisión] {n}" for n in notes]
            affected = _attributable(feedback, files)
            if it == config.MAX_ITERATIONS or not affected:
                break
            step(f"corrigiendo: {', '.join(affected)}")
            prompt = (f"{_plan_digest(plan)}\n\nREQUERIMIENTO ORIGINAL:\n{task['instruction']}\n\n"
                      f"ARCHIVOS A CORREGIR:\n{_context({p: files[p] for p in affected}, config.MAX_CONTEXT_CHARS)}\n\n"
                      "PROBLEMAS:\n" + "\n".join(f"- {f}" for f in feedback)
                      + "\n\nDevuelve completos SOLO los archivos corregidos, en el mismo formato.")
            fixed = _ask_files(llm, system_gen, prompt, affected, task, f"Corrección {it}")
            if not fixed:
                step("el modelo no devolvió archivos corregidos")
                break
            files.update(fixed)

        if candidate is None:
            return {"status": "failed", "model": model, "iterations": it,
                    "error": "No se obtuvo un proyecto que pase la validación estática", "errors": last_errors}
        report, notes, iterations = candidate
        commit = plan["commit"] or f"feat: crea proyecto {plan['name']}"
        result = engine.finish(task, ws, report, commit, notes, iterations, model, step)
        result["plan"] = task["plan"]
        return result
    except (LLMError, GitError) as exc:
        step(f"error: {exc}")
        try:
            ws.reset()
        except Exception:  # noqa: BLE001 - limpieza best-effort
            pass
        return {"status": "failed", "model": model, "error": str(exc)}
