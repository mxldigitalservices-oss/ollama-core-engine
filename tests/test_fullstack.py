"""Pruebas del modo proyecto, validadores full-stack, repos vacíos y el servidor/dashboard."""
import json
import os
import re
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from agent import config, engine, project, server, validators
from agent.gitops import Workspace


def sh(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class FullStackValidatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def v(self, files, **kw):
        return validators.validate_files(files, self.tmp, **kw)

    def test_javascript_truncado_y_valido(self):
        self.assertTrue(self.v({"a.js": "function f() {\n  if (x) {\n    return 1;\n"}).errors)
        ok = "const re = /['\"]/g;\nconst t = `a${1}`;\nfunction f() { return re.test(t); }\n"
        self.assertEqual(self.v({"a.js": ok}).errors, [])

    def test_css_html_y_jsx(self):
        self.assertTrue(self.v({"a.css": "a { color: red;"}).errors)
        self.assertTrue(self.v({"i.html": "<html><body><div><p>hola</p>"}).errors)
        self.assertTrue(self.v({"i.html": "<html><body><script>const a = (1;</script></body></html>"}).errors)
        self.assertEqual(self.v({"i.html": "<!doctype html><html><body><p>Hola<br><img src=x></body></html>"}).errors, [])
        r = self.v({"A.jsx": "export default function A() { return <p>Don't</p>; }\n"})  # apóstrofo en JSX
        self.assertEqual(r.errors, [])

    def test_dockerfile_y_yaml(self):
        self.assertEqual(self.v({"Dockerfile": "FROM node:20\nCMD [\"node\", \"a.js\"]\n"}, repo="cli/ente").errors, [])
        self.assertTrue(self.v({"Dockerfile": "RUN echo hola\n"}, repo="cli/ente").errors)
        self.assertTrue(self.v({"ci.yml": "a:\n\tb: 1\n"}).errors)

    def test_proteccion_depende_del_repo(self):
        with mock.patch.object(config, "PROTECTED_REPOS", ["acme/motor"]):
            motor = self.v({"railway.json": "{}", "agent/x.py": "X = 1\n"}, repo="acme/motor")
            cliente = self.v({"railway.json": "{}", "agent/x.py": "X = 1\n", ".env.example": "A=\n", "Dockerfile": "FROM x\n"}, repo="acme/cliente")
            self.assertEqual(len(motor.errors), 2)
            self.assertEqual(cliente.errors, [])
            self.assertTrue(self.v({".github/workflows/a.yml": "a: 1\n", ".env": "A=1\n"}, repo="acme/cliente").errors)

    def test_requirements_en_subcarpeta(self):
        r = self.v({"backend/main.py": "import fastapi\nprint(fastapi)\n", "backend/requirements.txt": "fastapi\n"})
        self.assertEqual(r.errors, [])


PLAN = {
    "name": "Barbería Demo", "summary": "Web de reservas.", "stack": ["Python", "FastAPI", "HTML"],
    "commit": "feat: crea web de barbería", "env": ["DATABASE_URL"], "run": ["uvicorn backend.main:app"],
    "files": [{"path": "backend/requirements.txt", "purpose": "dependencias"},
              {"path": "backend/main.py", "purpose": "API"},
              {"path": "frontend/index.html", "purpose": "interfaz"},
              {"path": "frontend/app.js", "purpose": "lógica"},
              {"path": "Dockerfile", "purpose": "imagen"}],
}
CONTENT = {
    "backend/requirements.txt": "fastapi\nuvicorn\n",
    "backend/main.py": "import os\n\nfrom fastapi import FastAPI\n\napp = FastAPI()\nDB = os.getenv('DATABASE_URL', '')\n\n\n@app.get('/health')\ndef health():\n    return {'ok': True, 'db': bool(DB)}\n",
    "frontend/index.html": "<!doctype html><html><body><h1>Barbería</h1><script src=\"app.js\"></script></body></html>\n",
    "frontend/app.js": "fetch('/health').then((r) => r.json()).then(console.log);\n",
    "Dockerfile": "FROM python:3.12-slim\nCMD [\"uvicorn\", \"backend.main:app\", \"--host\", \"0.0.0.0\"]\n",
}


def fake_llm(plan=PLAN, content=CONTENT, review="APROBADO", broken_once=None):
    state = {"broken": dict(broken_once or {}), "prompts": []}

    def llm(messages, temperature=0.1):
        system, user = messages[0]["content"], messages[-1]["content"]
        state["prompts"].append(user)
        if system.startswith("Eres un revisor"):
            return review
        if "MODO ARQUITECTO" in system:
            return "<analisis>Elijo FastAPI y HTML.</analisis>\n=== PLAN ===\n" + json.dumps(plan) + "\n=== END PLAN ==="
        wanted = re.search(r"SOLO estos archivos: (.+)$|Devuelve completos SOLO", user)
        paths = [p.strip() for p in wanted.group(1).split(",")] if wanted and wanted.group(1) else re.findall(r"^### (\S+)$", user, re.M)
        out = ["<analisis>Genero los archivos pedidos.</analisis>"]
        for p in paths:
            body = state["broken"].pop(p, None) if "CORREGIR" not in user else None
            text = body if body is not None else content[p]
            out.append(f"=== FILE: {p} ===\n{text.rstrip(chr(10))}\n=== END FILE ===")
        return "\n".join(out)
    llm.state = state
    return llm


class ProjectModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        remote = os.path.join(self.tmp, "remote", "acme")
        os.makedirs(remote)
        self.bare = os.path.join(remote, "cliente.git")
        sh("git", "init", "-q", "--bare", "-b", "main", self.bare)  # repo vacío, recién creado
        for k, v in {"WORKSPACE": os.path.join(self.tmp, "ws"),
                     "REMOTE_TEMPLATE": "file://" + os.path.join(self.tmp, "remote", "{repo}.git"),
                     "TARGET_BRANCH": "", "GITHUB_TOKEN": "", "API_TOKEN": "", "BASE_BRANCH": "",
                     "PROTECTED_REPOS": ["acme/motor"], "PROJECT_BATCH": 2, "WEB_ENABLED": False,
                     "KNOWLEDGE_DIR": os.path.join(self.tmp, "knowledge")}.items():
            p = mock.patch.object(config, k, v)
            p.start()
            self.addCleanup(p.stop)

    def run_p(self, llm, **extra):
        task = {"id": "p1", "instruction": "Créame una web para una barbería con reservas, API y base de datos",
                "repo": "acme/cliente", "mode": "project", "model": "qwen-test", **extra}
        return task, engine.run_task(task, Workspace("acme/cliente"), llm=llm)

    def test_proyecto_en_repo_vacio(self):
        llm = fake_llm()
        task, res = self.run_p(llm)
        self.assertEqual(res["status"], "done", res)
        self.assertEqual(res["branch"], "main")  # primer commit: crea la rama principal
        self.assertTrue(res["verified"])
        files = sh("git", "--git-dir", self.bare, "ls-tree", "-r", "--name-only", "main").split()
        for expected in (*CONTENT, "README.md", ".gitignore", ".env.example"):
            self.assertIn(expected, files)
        readme = sh("git", "--git-dir", self.bare, "show", "main:README.md")
        self.assertIn("DATABASE_URL", readme)
        self.assertEqual(sh("git", "--git-dir", self.bare, "log", "-1", "--format=%s", "main"), "feat: crea web de barbería")
        # lotes de 2 archivos + razonamiento y validación visibles para la interfaz
        self.assertGreaterEqual(len([r for r in task["reasoning"] if r["stage"].startswith("Lote")]), 3)
        self.assertTrue(task["validation"][-1]["ok"])
        self.assertEqual(res["model"], "qwen-test")

    def test_corrige_archivo_con_error(self):
        malo = {"backend/main.py": "from fastapi import FastAPI\napp = FastAPI(\n"}
        llm = fake_llm(broken_once=malo)
        task, res = self.run_p(llm)
        self.assertEqual(res["status"], "done", res)
        self.assertEqual(res["iterations"], 2)
        self.assertFalse(task["validation"][0]["ok"])
        self.assertIn("@app.get", sh("git", "--git-dir", self.bare, "show", "main:backend/main.py"))

    def test_plan_con_ruta_peligrosa_falla(self):
        plan = {**PLAN, "files": [{"path": "../fuera.py", "purpose": "x"}]}
        _, res = self.run_p(fake_llm(plan=plan))
        self.assertEqual(res["status"], "failed")
        self.assertEqual(sh("git", "--git-dir", self.bare, "branch", "--list"), "")

    def test_dry_run_no_publica(self):
        _, res = self.run_p(fake_llm(), dry_run=True)
        self.assertEqual(res["status"], "done")
        self.assertIn("README.md", res["contents"])
        self.assertEqual(sh("git", "--git-dir", self.bare, "branch", "--list"), "")

    def test_parse_plan(self):
        plan, problems = project.parse_plan("=== PLAN ===\n" + json.dumps(PLAN) + "\n=== END PLAN ===", [])
        self.assertEqual(problems, [])
        self.assertEqual(plan["name"], "barber-a-demo")
        self.assertIsNone(project.parse_plan("sin plan", [])[0])


class ModelListTests(unittest.TestCase):
    def test_latest_se_normaliza(self):
        from agent import llm
        body = json.dumps({"models": [{"name": "elite-architect:latest"}, {"name": "qwen2.5-coder:7b"}]}).encode()
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = body
        with mock.patch("urllib.request.urlopen", return_value=resp):
            self.assertEqual(llm.installed_models(), ["elite-architect", "qwen2.5-coder:7b"])
        with mock.patch("urllib.request.urlopen", side_effect=OSError("sin red")):
            self.assertEqual(llm.installed_models(), [])


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.patches = [mock.patch.object(config, "API_TOKEN", "tok-de-prueba-123"),
                       mock.patch.object(config, "GITHUB_REPO", "acme/cliente"),
                       mock.patch.object(config, "ALLOWED_REPOS", ["acme/cliente"]),
                       mock.patch.object(server, "installed_models", lambda: ["elite-architect:latest", "qwen2.5-coder:7b"])]
        for p in cls.patches:
            p.start()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        for p in cls.patches:
            p.stop()

    def get(self, path, token=True):
        req = urllib.request.Request(self.base + path, headers={"Authorization": "Bearer tok-de-prueba-123"} if token else {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def test_dashboard_publico_con_csp(self):
        for path in ("/ui", "/ui/app.js"):
            status, headers, body = self.get(path, token=False)
            self.assertEqual(status, 200)
            self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
            self.assertNotIn(b"tok-de-prueba", body)
        self.assertIn(self.get("/ui/../agent/server.py", token=False)[0], (401, 404))  # solo archivos de la lista blanca
        self.assertEqual(self.get("/ui/server.py")[0], 404)

    def test_api_exige_token_y_no_filtra_secretos(self):
        self.assertEqual(self.get("/agent/config", token=False)[0], 401)
        status, _, body = self.get("/agent/config")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertIn("qwen2.5-coder:7b", data["models"]["installed"])
        self.assertTrue(next(e for e in data["env"] if e["name"] == "API_TOKEN")["set"])
        self.assertNotIn(b"tok-de-prueba", body)

    def test_validacion_de_solicitud(self):
        base = {"instruction": "crea una API de clientes con FastAPI"}
        ok, err = server._validate_request({**base, "mode": "project", "model": "qwen2.5-coder:7b"})
        self.assertIsNone(err)
        self.assertEqual((ok["mode"], ok["model"]), ("project", "qwen2.5-coder:7b"))
        self.assertIn("no está instalado", server._validate_request({**base, "model": "otro:1b"})[1])
        self.assertIn("mode", server._validate_request({**base, "mode": "raro"})[1])
        self.assertIn("repo", server._validate_request({**base, "repo": "x/y"})[1])


if __name__ == "__main__":
    unittest.main()
