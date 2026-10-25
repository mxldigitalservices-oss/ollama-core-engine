"""Autogestión Git: aplica archivos ya validados, hace add + commit claro + push.

Lo usa el motor del agente y también sirve como herramienta de terminal:

    python3 -m agent.autogit --repo owner/nombre [--source DIR] [-m "feat: ..."] [--dry-run] ruta1 ruta2
"""
from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import sys

from . import config, validators
from .gitops import GitError, Workspace

_CONVENTIONAL = re.compile(r"^(feat|fix|docs|refactor|test|chore|perf|build|ci|style)(\([\w./-]+\))?!?: \S")
_REJECTED = ("rejected", "non-fast-forward", "fetch first", "failed to push")


def default_subject(new_map: dict[str, bool]) -> str:
    paths = list(new_map)
    what = paths[0] if len(paths) == 1 else f"{len(paths)} archivos"
    if all(p.lower().endswith(".md") for p in paths):
        return f"docs: actualiza {what}"[:72]
    if all(new_map.values()):
        return f"feat: agrega {what}"[:72]
    return f"chore: actualiza {what}"[:72]


def build_commit_message(raw: str, report: validators.Report, model: str, new_map: dict[str, bool]) -> str:
    lines = [ln.rstrip() for ln in re.sub(r"[\x00-\x08\x0b-\x1f]", "", raw or "").strip().splitlines()]
    subject = re.sub(r"\s+", " ", lines[0]).strip() if lines else ""
    if not subject:
        subject = default_subject(new_map)
    elif not _CONVENTIONAL.match(subject):
        subject = f"{'feat' if all(new_map.values()) else 'chore'}: {subject}"
    subject = subject[:72].rstrip()
    body = "\n".join(lines[1:]).strip()
    changes = "Cambios:\n" + "\n".join(
        f"- {'agrega' if is_new else 'actualiza'} {path}" for path, is_new in new_map.items()
    )
    footer = (
        f"Validación automática: {', '.join(report.tools)}, secretos "
        f"({len(report.warnings)} advertencia(s)).\n"
        f"Generado por ollama-core-engine (modelo {model})."
    )
    return "\n\n".join(p for p in (subject, body, changes, footer) if p) + "\n"


def publish(ws: Workspace, report: validators.Report, commit_raw: str, *, retries: int = 1, model: str | None = None) -> dict | None:
    """Aplica los archivos validados, hace git add/commit/push. None = no había cambios.

    Si el push es rechazado porque la rama avanzó, re-sincroniza, reaplica y reintenta.
    """
    for attempt in range(retries + 1):
        new_map = {p: not os.path.exists(os.path.join(ws.root, p)) for p in report.files}
        paths = ws.apply(report.files)
        message = build_commit_message(commit_raw, report, model or config.MODEL, new_map)
        try:
            return ws.commit_and_push(paths, message, slug_hint=message.splitlines()[0])
        except GitError as exc:
            if attempt < retries and any(tok in str(exc).lower() for tok in _REJECTED):
                ws.sync()
                continue
            raise
    return None  # pragma: no cover


# ----------------------------------------------------------------- CLI

def _read_sources(source: str, paths: list[str]) -> dict[str, str]:
    root = os.path.realpath(source)
    files: dict[str, str] = {}
    for rel in paths:
        full = os.path.realpath(os.path.join(root, rel))
        if not full.startswith(root + os.sep) or not os.path.isfile(full):
            raise GitError(f"No se encontró {rel} dentro de {source}")
        with open(full, encoding="utf-8") as fh:
            files[posixpath.normpath(rel.replace(os.sep, "/"))] = fh.read()
    return files


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m agent.autogit", description=__doc__.split("\n")[0])
    ap.add_argument("paths", nargs="+", help="archivos (relativos a --source) que se publicarán con la misma ruta")
    ap.add_argument("--repo", default=config.GITHUB_REPO, help="owner/nombre (por defecto GITHUB_REPO)")
    ap.add_argument("--source", default=".", help="carpeta donde están los archivos")
    ap.add_argument("-m", "--message", default="", help="mensaje de commit (si falta, se genera)")
    ap.add_argument("--dry-run", action="store_true", help="solo valida")
    args = ap.parse_args(argv)

    if args.repo not in config.ALLOWED_REPOS:
        print("repo no permitido (ver AGENT_ALLOWED_REPOS)", file=sys.stderr)
        return 2
    try:
        files = _read_sources(args.source, args.paths)
        ws = Workspace(args.repo)
        ws.sync()
        report = validators.validate_files(files, ws.root, repo=args.repo, secret_values=(config.API_TOKEN, config.GITHUB_TOKEN))
        if report.errors:
            print("Validación fallida; no se sube nada:", file=sys.stderr)
            for issue in report.errors:
                print(f"  {issue}", file=sys.stderr)
            return 1
        for issue in report.warnings:
            print(f"  advertencia {issue}", file=sys.stderr)
        if args.dry_run:
            print(json.dumps({"status": "ok", "dry_run": True, "files": list(report.files)}, ensure_ascii=False))
            return 0
        info = publish(ws, report, args.message)
    except (GitError, UnicodeDecodeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(info or {"status": "no_changes"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
