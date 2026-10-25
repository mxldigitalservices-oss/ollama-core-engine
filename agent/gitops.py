"""Operaciones Git del agente. El token nunca va en la URL ni en los logs."""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import time
import unicodedata

from . import config

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


class GitError(RuntimeError):
    pass


def slugify(text: str, limit: int = 30) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug[:limit].strip("-") or "cambios"


class Workspace:
    def __init__(self, repo: str):
        if not _REPO_RE.match(repo):
            raise GitError(f"Nombre de repositorio inválido: {repo!r}")
        self.repo = repo
        self.root = os.path.join(config.WORKSPACE, repo.replace("/", "__"))
        self.base = config.BASE_BRANCH or "main"
        self.empty = False  # True si el remoto aún no tiene ninguna rama (repo recién creado)

    # ------------------------------------------------------------ interno
    def _askpass(self) -> str:
        os.makedirs(config.WORKSPACE, exist_ok=True)
        path = os.path.join(config.WORKSPACE, ".askpass.sh")
        script = (
            "#!/bin/sh\n"
            'case "$1" in\n'
            '  Username*) echo "x-access-token" ;;\n'
            '  *) echo "$GITHUB_TOKEN" ;;\n'
            "esac\n"
        )
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
        with os.fdopen(fd, "w") as fh:
            fh.write(script)
        os.chmod(path, stat.S_IRWXU)
        return path

    def _env(self) -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/root"),
            "LC_ALL": "C",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": self._askpass(),
            "GITHUB_TOKEN": config.GITHUB_TOKEN,
        }

    @staticmethod
    def _redact(text: str) -> str:
        tok = config.GITHUB_TOKEN
        return text.replace(tok, "***") if tok else text

    def _run(self, *args: str, cwd: str | None = None, input: str | None = None) -> tuple[int, str]:
        try:
            r = subprocess.run(
                ["git", *args], cwd=cwd or self.root, env=self._env(), input=input,
                capture_output=True, text=True, timeout=config.GIT_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git {args[0]} excedió el tiempo límite") from exc
        return r.returncode, self._redact((r.stdout or "") + (r.stderr or "")).strip()

    def _git(self, *args: str, cwd: str | None = None, input: str | None = None, check: bool = True) -> str:
        rc, out = self._run(*args, cwd=cwd, input=input)
        if check and rc != 0:
            raise GitError(f"git {args[0]} falló: {out[:500]}")
        return out

    def _safe_dest(self, rel: str) -> str:
        root_real = os.path.realpath(self.root)
        dest = os.path.realpath(os.path.join(self.root, rel))
        if not dest.startswith(root_real + os.sep):
            raise GitError(f"Ruta fuera del repositorio: {rel}")
        return dest

    # ------------------------------------------------------------ público
    def sync(self) -> None:
        """Deja el workspace limpio y alineado con la rama base del remoto."""
        os.makedirs(config.WORKSPACE, exist_ok=True)
        url = config.REMOTE_TEMPLATE.format(repo=self.repo)
        if not os.path.isdir(os.path.join(self.root, ".git")):
            if os.path.exists(self.root):
                shutil.rmtree(self.root)
            self._git("clone", "--depth", "1", url, self.root, cwd=config.WORKSPACE)
        self._git("remote", "set-url", "origin", url)
        rc, heads = self._run("ls-remote", "--heads", "origin")
        self.empty = rc == 0 and not heads.strip()
        if self.empty:
            # Repo vacío: HEAD sin commits y árbol limpio; el primer push crea la rama base.
            self._git("symbolic-ref", "HEAD", "refs/heads/agent-work")
            self._git("read-tree", "--empty")
            self._git("clean", "-fdq")
            self.base = config.BASE_BRANCH or "main"
            return
        base = config.BASE_BRANCH
        if not base:
            head = self._git("symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False)
            base = head.split("/", 1)[1] if head.startswith("origin/") else "main"
        self._git("fetch", "--depth", "1", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}")
        start = f"origin/{base}"
        target = config.TARGET_BRANCH
        if target and target != base:
            if not _BRANCH_RE.match(target) or ".." in target:
                raise GitError(f"Nombre de rama inválido: {target!r}")
            rc, _ = self._run("ls-remote", "--exit-code", "--heads", "origin", target)
            if rc == 0:  # la rama destino ya existe: se continúa sobre su historial
                self._git("fetch", "--depth", "1", "origin", f"+refs/heads/{target}:refs/remotes/origin/{target}")
                start = f"origin/{target}"
        self._git("checkout", "-q", "-f", "-B", "agent-work", start)
        self._git("clean", "-fdq")
        self.base = base

    def tree(self, limit: int = 60) -> list[str]:
        out = self._git("ls-files", check=False)
        return out.splitlines()[:limit]

    def read(self, rel: str, max_chars: int) -> str | None:
        try:
            dest = self._safe_dest(rel)
        except GitError:
            return None
        if not os.path.isfile(dest):
            return None
        with open(dest, encoding="utf-8", errors="replace") as fh:
            text = fh.read(max_chars + 1)
        return text[:max_chars] + "\n[... truncado ...]" if len(text) > max_chars else text

    def apply(self, files: dict[str, str]) -> list[str]:
        written: list[str] = []
        for rel, content in files.items():
            dest = self._safe_dest(rel)
            if os.path.isdir(dest):
                raise GitError(f"{rel} es un directorio")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(content)
            if rel.endswith(".sh") and content.startswith("#!"):
                os.chmod(dest, 0o755)
            written.append(rel)
        return written

    def reset(self) -> None:
        self._git("reset", "--hard", "-q", check=False)
        self._git("clean", "-fdq", check=False)

    def commit_and_push(self, paths: list[str], message: str, *, slug_hint: str = "", push: bool = True) -> dict | None:
        self._git("add", "--", *paths)
        if not self._git("diff", "--cached", "--name-only", "--", *paths):
            return None
        self._git(
            "-c", f"user.name={config.GIT_NAME}", "-c", f"user.email={config.GIT_EMAIL}",
            "commit", "-q", "-F", "-", input=message,
        )
        sha = self._git("rev-parse", "HEAD")
        if config.TARGET_BRANCH:
            branch = config.TARGET_BRANCH
        elif self.empty:
            branch = self.base  # primer commit del repo: queda como rama por defecto
        else:
            branch = f"agent/{time.strftime('%Y%m%d-%H%M%S')}-{slugify(slug_hint)}"
        if not _BRANCH_RE.match(branch) or ".." in branch:
            raise GitError(f"Nombre de rama inválido: {branch!r}")
        verified = False
        if push:
            self._git("push", "origin", f"HEAD:refs/heads/{branch}")
            remote = self._git("ls-remote", "--heads", "origin", f"refs/heads/{branch}", check=False)
            verified = remote.split()[:1] == [sha]  # confirma que GitHub recibió exactamente este commit
            if not verified:
                raise GitError("el push terminó sin error pero el remoto no muestra el commit esperado")
        url = None
        if config.REMOTE_TEMPLATE.startswith("https://github.com/"):
            url = f"https://github.com/{self.repo}/tree/{branch}"
        return {"commit": sha, "branch": branch, "base": self.base, "pushed": push, "verified": verified, "url": url}
