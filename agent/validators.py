"""Validación estática del código generado. Nunca ejecuta el código del modelo."""
from __future__ import annotations

import ast
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

from . import config

try:  # pyflakes viene de apt (python3-pyflakes) en la imagen Docker
    from pyflakes.checker import Checker as _PyflakesChecker
except ImportError:  # pragma: no cover
    _PyflakesChecker = None


@dataclass
class Issue:
    path: str
    kind: str
    message: str
    line: int | None = None

    def __str__(self) -> str:
        loc = f":{self.line}" if self.line else ""
        return f"[{self.kind}] {self.path}{loc}: {self.message}"


@dataclass
class Report:
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)  # rutas ya normalizadas


# nombre de import -> nombre del paquete en requirements.txt
IMPORT_ALIASES = {
    "yaml": "pyyaml", "PIL": "pillow", "jwt": "pyjwt", "bs4": "beautifulsoup4",
    "dotenv": "python-dotenv", "sklearn": "scikit-learn", "cv2": "opencv-python",
    "psycopg2": "psycopg2-binary", "MySQLdb": "mysqlclient", "jose": "python-jose",
    "multipart": "python-multipart", "dateutil": "python-dateutil", "magic": "python-magic",
    "OpenSSL": "pyopenssl", "Crypto": "pycryptodome", "serial": "pyserial",
    "attr": "attrs", "git": "gitpython", "fitz": "pymupdf", "docx": "python-docx",
    "pptx": "python-pptx", "googleapiclient": "google-api-python-client",
}

# Mensajes de pyflakes que no bloquean (el resto sí).
PYFLAKES_WARN = {
    "UnusedImport", "UnusedVariable", "UnusedAnnotation", "RedefinedWhileUnused",
    "ImportStarUsage", "UnusedIndirectAssignment",
}

SECRET_PATTERNS = [
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"), "token de GitHub"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "token de GitHub"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "clave de acceso de AWS"),
    (re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"), "llave privada"),
    (re.compile(r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|rediss|amqp)://[^\s:@/'\"]+:[^\s@/'\"]+@"),
     "URL de base de datos con credenciales"),
]


def _norm_pkg(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


# ---------------------------------------------------------------- rutas

def check_path(raw: str, protected: list[str], allowed_ext: set[str],
               allowed_names: set[str] | frozenset[str] = frozenset()) -> tuple[str | None, Issue | None]:
    p = raw.strip().strip("`'\"")
    if not p:
        return None, Issue(raw, "ruta", "ruta vacía")
    if "\x00" in p or "\\" in p or p.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", p):
        return None, Issue(p, "ruta", "ruta absoluta o con caracteres no permitidos")
    norm = posixpath.normpath(p)
    if norm in (".", "..") or norm.startswith("../"):
        return None, Issue(p, "ruta", "la ruta sale de la raíz del repositorio")
    parts = norm.split("/")
    low = norm.lower()
    if ".git" in [x.lower() for x in parts]:
        return None, Issue(norm, "ruta", "no se permite escribir dentro de .git")
    if parts[-1].lower().startswith(".env") and parts[-1].lower() not in allowed_names:
        return None, Issue(norm, "ruta", "no se permite escribir archivos .env")
    for pr in protected:
        prl = pr.lower()
        if prl.endswith("/"):
            hit = low.startswith(prl) or low == prl[:-1]
        else:
            hit = low == prl or low.startswith(prl + "/")
        if hit:
            return None, Issue(norm, "ruta", f"ruta protegida ({pr}); el agente no puede modificarla")
    ext = posixpath.splitext(norm)[1].lower()
    if ext not in allowed_ext and parts[-1].lower() not in allowed_names:
        return None, Issue(norm, "ruta", f"extensión '{ext or '(ninguna)'}' no permitida")
    return norm, None


# ------------------------------------------------------- resolución de imports

class ImportResolver:
    def __init__(self, repo_root: str, new_files: dict[str, str]):
        self.root = repo_root
        self.new = dict(new_files)
        self.reqs: set[str] = set()
        self.manifest_text = ""
        self.has_requirements = False
        self._load_manifests()

    def _load_manifests(self) -> None:
        texts: list[str] = []
        try:
            names = os.listdir(self.root)
        except OSError:
            names = []
        for fn in names:
            full = os.path.join(self.root, fn)
            if re.fullmatch(r"requirements[\w.-]*\.txt", fn) and os.path.isfile(full):
                texts.append(self._read(full))
                self.has_requirements = True
            elif fn in ("pyproject.toml", "Pipfile", "setup.py", "setup.cfg") and os.path.isfile(full):
                self.manifest_text += "\n" + self._read(full)
        for rel, content in self.new.items():
            base = posixpath.basename(rel)
            if re.fullmatch(r"requirements[\w.-]*\.txt", base):  # también backend/requirements.txt
                texts.append(content)
                self.has_requirements = True
        for text in texts:
            for line in text.splitlines():
                line = line.split("#", 1)[0].strip()
                if not line or line.startswith(("-", "git+", "http")):
                    continue
                name = re.split(r"[\s<>=!~;@\[]", line, 1)[0]
                if name:
                    self.reqs.add(_norm_pkg(name))
        self.manifest_text = _norm_pkg_text(self.manifest_text)

    @staticmethod
    def _read(path: str) -> str:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                return fh.read(200_000)
        except OSError:
            return ""

    def _exists(self, rel: str) -> bool:
        return rel in self.new or os.path.exists(os.path.join(self.root, rel))

    def _is_pkg_dir(self, rel: str) -> bool:
        prefix = rel.rstrip("/") + "/"
        return any(k.startswith(prefix) for k in self.new) or os.path.isdir(os.path.join(self.root, rel))

    def _local(self, top: str, from_dir: str) -> bool:
        bases = {"", "src"}
        d = from_dir
        while d:
            bases.add(d)
            d = posixpath.dirname(d)
        for base in bases:
            cand = posixpath.join(base, top) if base else top
            if self._exists(cand + ".py") or self._is_pkg_dir(cand):
                return True
        return False

    def relative_ok(self, from_dir: str, level: int, module: str | None) -> bool | None:
        """True/False si se puede decidir; None si no hay nada que comprobar."""
        base = from_dir
        for _ in range(level - 1):
            if not base:
                return False
            base = posixpath.dirname(base)
        if not module:
            return None
        cand = posixpath.join(base, *module.split("."))
        return self._exists(cand + ".py") or self._is_pkg_dir(cand)

    def absolute_status(self, top: str, from_dir: str) -> str:
        """'ok', 'unknown' (no verificable: hay manifiesto no requirements) o 'missing'."""
        if top in sys.stdlib_module_names or self._local(top, from_dir):
            return "ok"
        n = _norm_pkg(top)
        alias = _norm_pkg(IMPORT_ALIASES.get(top, top))
        if n in self.reqs or alias in self.reqs:
            return "ok"
        if self.manifest_text and (n in self.manifest_text or alias in self.manifest_text):
            return "ok"
        if not self.has_requirements and self.manifest_text:
            return "unknown"
        return "missing"


def _norm_pkg_text(text: str) -> str:
    return re.sub(r"[-_.]+", "-", text).lower()


# ------------------------------------------------------------------ Python

def _guarded_imports(tree: ast.AST) -> set[int]:
    guarded: set[int] = set()
    catchers = {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        catches = False
        for h in node.handlers:
            if h.type is None:
                catches = True
                continue
            elts = h.type.elts if isinstance(h.type, ast.Tuple) else [h.type]
            if any(isinstance(e, ast.Name) and e.id in catchers for e in elts):
                catches = True
        if catches:
            for stmt in node.body:
                for sub in ast.walk(stmt):
                    if isinstance(sub, (ast.Import, ast.ImportFrom)):
                        guarded.add(id(sub))
    return guarded


def check_python(rel: str, content: str, resolver: ImportResolver) -> tuple[list[Issue], list[Issue]]:
    errors: list[Issue] = []
    warnings: list[Issue] = []
    try:
        tree = ast.parse(content, filename=rel)
    except SyntaxError as exc:
        return [Issue(rel, "sintaxis", f"{exc.msg}", exc.lineno)], []
    except (ValueError, RecursionError) as exc:
        return [Issue(rel, "sintaxis", f"no se pudo analizar: {exc}")], []

    from_dir = posixpath.dirname(rel)
    guarded = _guarded_imports(tree)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)) or id(node) in guarded:
            continue
        if isinstance(node, ast.ImportFrom) and node.level:
            status = resolver.relative_ok(from_dir, node.level, node.module)
            if status is False:
                errors.append(Issue(rel, "import", f"import relativo no resuelve: {'.' * node.level}{node.module or ''}", node.lineno))
            continue
        names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
        for full in names:
            top = full.split(".")[0]
            if not top:
                continue
            status = resolver.absolute_status(top, from_dir)
            if status == "missing":
                errors.append(Issue(
                    rel, "import",
                    f"'{top}' no es stdlib, ni módulo del repo, ni está en requirements.txt", node.lineno))
            elif status == "unknown":
                warnings.append(Issue(rel, "import", f"no se pudo verificar la dependencia '{top}'", node.lineno))

    if _PyflakesChecker is not None:
        checker = _PyflakesChecker(tree, filename=rel)
        for msg in sorted(checker.messages, key=lambda m: m.lineno):
            kind = type(msg).__name__
            issue = Issue(rel, "pyflakes", f"{msg.message % msg.message_args} ({kind})", msg.lineno)
            (warnings if kind in PYFLAKES_WARN else errors).append(issue)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in ("eval", "exec"):
                warnings.append(Issue(rel, "seguridad", f"uso de {fn.id}()", node.lineno))
            elif isinstance(fn, ast.Attribute) and fn.attr == "system" and isinstance(fn.value, ast.Name) and fn.value.id == "os":
                warnings.append(Issue(rel, "seguridad", "uso de os.system()", node.lineno))
            for kw in node.keywords:
                if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                    warnings.append(Issue(rel, "seguridad", "subprocess con shell=True", node.lineno))
    return errors, warnings


# ----------------------------------------------------------- otros formatos

def check_shell(rel: str, content: str) -> tuple[list[Issue], list[Issue]]:
    first = content.split("\n", 1)[0]
    shell = "bash" if "bash" in first else "sh"
    exe = shutil.which(shell)
    if not exe:
        return [], [Issue(rel, "sintaxis", f"no se pudo validar: no hay {shell} disponible")]
    try:
        r = subprocess.run([exe, "-n"], input=content, capture_output=True, text=True, timeout=10)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return [], [Issue(rel, "sintaxis", f"no se pudo validar el script: {exc}")]
    if r.returncode != 0:
        return [Issue(rel, "sintaxis", (r.stderr or "error de sintaxis").strip()[:300])], []
    return [], []


def check_structured(rel: str, content: str) -> list[Issue]:
    ext = posixpath.splitext(rel)[1].lower()
    try:
        if ext in (".json", ".webmanifest"):
            json.loads(content)
        elif ext == ".toml":
            import tomllib  # Python 3.11+
            tomllib.loads(content)
        elif ext in (".xml", ".xsd", ".xsl", ".xslt", ".svg"):
            if "<!ENTITY" in content:
                return [Issue(rel, "seguridad", "se rechazan entidades XML (<!ENTITY)")]
            import xml.etree.ElementTree as ET
            ET.fromstring(content.encode("utf-8"))
    except ImportError:
        return []
    except json.JSONDecodeError as exc:
        return [Issue(rel, "sintaxis", f"JSON inválido: {exc.msg}", exc.lineno)]
    except Exception as exc:  # TOMLDecodeError, ParseError
        return [Issue(rel, "sintaxis", f"formato inválido: {exc}")]
    return []


_OPENERS = {"(": ")", "[": "]", "{": "}"}
_CLOSERS = {v: k for k, v in _OPENERS.items()}
_REGEX_PREV = set("(,=:[!&|?{};+-*%<>~^") | {""}


def _code_skeleton(text: str, css: bool = False) -> str:
    """Devuelve solo los delimitadores de código, ignorando comentarios, cadenas y regex."""
    out: list[str] = []
    i, n, prev = 0, len(text), ""
    while i < n:
        c = text[i]
        two = text[i:i + 2]
        if two == "/*":
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if two == "//" and not css:
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if c in "'\"`":
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == "\\" else 1
                if c != "`" and j < n and text[j] == "\n":
                    break
            i, prev = j + 1, "x"
            continue
        if c == "/" and not css and prev in _REGEX_PREV:  # literal regex
            j, in_cls = i + 1, False
            while j < n and text[j] != "\n" and (in_cls or text[j] != "/"):
                if text[j] == "\\":
                    j += 1
                elif text[j] == "[":
                    in_cls = True
                elif text[j] == "]":
                    in_cls = False
                j += 1
            i, prev = j + 1, "x"
            continue
        if c in _OPENERS or c in _CLOSERS:
            out.append(c)
        if not c.isspace():
            prev = c
        i += 1
    return "".join(out)


def check_brackets(rel: str, content: str, *, strict: bool, css: bool = False) -> list[Issue]:
    """Detecta llaves/paréntesis sin cerrar (típico de una respuesta truncada por el modelo)."""
    stack: list[str] = []
    for ch in _code_skeleton(content, css):
        if ch in _OPENERS:
            stack.append(ch)
        elif not stack or stack.pop() != _CLOSERS[ch]:
            return [Issue(rel, "sintaxis", f"'{ch}' sin su apertura correspondiente")]
    if stack:
        return [Issue(rel, "sintaxis", f"'{stack[-1]}' sin cerrar: ¿archivo truncado?")] if strict else []
    return []


_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
_MUST_CLOSE = {"html", "body", "head", "div", "script", "style", "section", "main", "form", "table", "nav", "header", "footer"}


def check_html(rel: str, content: str) -> tuple[list[Issue], list[Issue]]:
    from html.parser import HTMLParser

    stack: list[str] = []
    warns: list[Issue] = []

    class P(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag not in _VOID:
                stack.append(tag)

        def handle_endtag(self, tag):
            if tag in stack:
                while stack and stack[-1] != tag:
                    stack.pop()
                stack.pop()
            elif tag not in _VOID:
                warns.append(Issue(rel, "html", f"</{tag}> sin etiqueta de apertura", self.getpos()[0]))

    text = re.sub(r"<%[\s\S]*?%>", "", content) if rel.lower().endswith(".ejs") else content
    p = P(convert_charrefs=True)
    try:
        p.feed(text)
        p.close()
    except Exception as exc:  # noqa: BLE001 - html.parser es tolerante; por si acaso
        return [Issue(rel, "sintaxis", f"HTML no analizable: {exc}")], warns
    open_tags = [t for t in stack if t in _MUST_CLOSE]
    if open_tags:
        return [Issue(rel, "sintaxis", f"HTML con <{open_tags[-1]}> sin cerrar: ¿archivo truncado?")], warns[:5]
    return [], warns[:5]


def check_dockerfile(rel: str, content: str) -> list[Issue]:
    for ln in content.splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            if not re.match(r"(?i)^(FROM|ARG|# syntax)", ln):
                return [Issue(rel, "sintaxis", "un Dockerfile debe empezar con FROM (o ARG)")]
            break
    else:
        return [Issue(rel, "sintaxis", "Dockerfile vacío")]
    if not re.search(r"(?im)^\s*FROM\s+\S+", content):
        return [Issue(rel, "sintaxis", "Dockerfile sin instrucción FROM")]
    return []


def check_yaml_basic(rel: str, content: str) -> list[Issue]:
    for lineno, line in enumerate(content.splitlines(), 1):
        if line.startswith("\t") or re.match(r" *\t", line):
            return [Issue(rel, "sintaxis", "YAML no admite tabuladores para indentar", lineno)]
    return []


def scan_secrets(rel: str, content: str, secret_values: tuple[str, ...]) -> list[Issue]:
    issues: list[Issue] = []
    for lineno, line in enumerate(content.splitlines(), 1):
        for pattern, label in SECRET_PATTERNS:
            if pattern.search(line):
                issues.append(Issue(rel, "seguridad", f"posible secreto ({label}); usa variables de entorno", lineno))
        for value in secret_values:
            if value and len(value) >= 8 and value in line:
                issues.append(Issue(rel, "seguridad", "contiene el valor de un secreto del servidor", lineno))
    return issues


# ------------------------------------------------------------ orquestación

def validate_files(files: dict[str, str], repo_root: str, *, secret_values: tuple[str, ...] = (),
                   repo: str | None = None, max_files: int | None = None) -> Report:
    report = Report()
    limit = max_files or config.MAX_FILES
    if len(files) > limit:
        report.errors.append(Issue("(respuesta)", "límite", f"máximo {limit} archivos por tarea"))
        return report
    # Repos propios: todas las rutas protegidas. Repos de proyecto: solo lo esencial.
    protected = config.PROTECTED_PATHS if repo is None or repo in config.PROTECTED_REPOS else config.ALWAYS_PROTECTED

    clean: dict[str, str] = {}
    for raw, content in files.items():
        rel, issue = check_path(raw, protected, config.ALLOWED_EXT, config.ALLOWED_NAMES)
        if issue:
            report.errors.append(issue)
            continue
        if len(content.encode("utf-8", "replace")) > config.MAX_FILE_BYTES:
            report.errors.append(Issue(rel, "límite", f"supera {config.MAX_FILE_BYTES} bytes"))
            continue
        clean[rel] = content
    report.files = clean
    if report.errors:
        return report

    resolver = ImportResolver(repo_root, clean)
    report.tools.append("ast+imports")
    if _PyflakesChecker is not None:
        report.tools.append("pyflakes")
    for rel, content in clean.items():
        ext = posixpath.splitext(rel)[1].lower()
        if ext == ".py":
            errs, warns = check_python(rel, content, resolver)
            report.errors += errs
            report.warnings += warns
        elif ext == ".sh":
            errs, warns = check_shell(rel, content)
            report.errors += errs
            report.warnings += warns
        elif ext in (".html", ".ejs"):
            errs, warns = check_html(rel, content)
            report.errors += errs
            report.warnings += warns
            if ext == ".html":  # scripts en línea: mismo control de delimitadores que los .js
                for m in re.finditer(r"(?is)<script(?P<attrs>[^>]*)>(?P<code>.*?)</script>", content):
                    if "src=" not in m.group("attrs").lower() and "json" not in m.group("attrs").lower():
                        report.errors += check_brackets(rel, m.group("code"), strict=True)
        elif ext in (".js", ".mjs", ".cjs", ".ts"):
            report.errors += check_brackets(rel, content, strict=True)
        elif ext in (".jsx", ".tsx"):  # JSX mezcla texto con apóstrofos: solo advertencia
            report.warnings += check_brackets(rel, content, strict=True)
        elif ext == ".css":
            report.errors += check_brackets(rel, content, strict=True, css=True)
        elif posixpath.basename(rel).lower() == "dockerfile":
            report.errors += check_dockerfile(rel, content)
        elif ext in (".yml", ".yaml"):
            report.errors += check_yaml_basic(rel, content)
        else:
            report.errors += check_structured(rel, content)
        report.errors += scan_secrets(rel, content, secret_values)
    return report
