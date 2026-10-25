"""Pruebas: python3 -m unittest discover -s tests -v"""
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from agent import autogit, config, engine, validators
from agent.gitops import Workspace


def sh(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class ValidatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def run_v(self, files):
        return validators.validate_files(files, self.tmp)

    def kinds(self, report):
        return {i.kind for i in report.errors}

    def test_codigo_valido(self):
        r = self.run_v({"app/main.py": "import os\n\n\ndef f():\n    return os.getcwd()\n"})
        self.assertEqual(r.errors, [])

    def test_error_de_sintaxis(self):
        r = self.run_v({"a.py": "def f(:\n    pass\n"})
        self.assertIn("sintaxis", self.kinds(r))

    def test_import_inexistente(self):
        r = self.run_v({"a.py": "import fastapi\n"})
        self.assertIn("import", self.kinds(r))

    def test_import_declarado_en_requirements(self):
        r = self.run_v({"a.py": "import yaml\nimport fastapi\nprint(yaml, fastapi)\n",
                        "requirements.txt": "fastapi==0.110\npyyaml>=6\n"})
        self.assertEqual(r.errors, [])

    def test_import_local_y_relativo(self):
        r = self.run_v({"pkg/__init__.py": "", "pkg/util.py": "X = 1\n",
                        "pkg/main.py": "from . import util\nfrom .util import X\nimport pkg.util\nprint(X, util, pkg)\n"})
        self.assertEqual(r.errors, [])

    def test_import_relativo_roto(self):
        r = self.run_v({"pkg/__init__.py": "", "pkg/main.py": "from .nada import X\nprint(X)\n"})
        self.assertIn("import", self.kinds(r))

    def test_import_protegido_por_try(self):
        r = self.run_v({"a.py": "try:\n    import ujson as json\nexcept ImportError:\n    import json\nprint(json)\n"})
        self.assertEqual(r.errors, [])

    def test_nombre_indefinido(self):
        r = self.run_v({"a.py": "print(variable_que_no_existe)\n"})
        self.assertIn("pyflakes", self.kinds(r))

    def test_import_sin_usar_es_advertencia(self):
        r = self.run_v({"a.py": "import os\n"})
        self.assertEqual(r.errors, [])
        self.assertTrue(r.warnings)

    def test_secretos(self):
        r = self.run_v({"a.py": 'URL = "postgresql://admin:SuperClave@db:5432/x"\n'})
        self.assertIn("seguridad", self.kinds(r))
        r = validators.validate_files({"a.py": 'T = "valor-secreto-123"\n'}, self.tmp, secret_values=("valor-secreto-123",))
        self.assertIn("seguridad", self.kinds(r))

    def test_rutas_peligrosas(self):
        for bad in ("../x.py", "/etc/x.py", ".github/workflows/a.yml", "agent/engine.py",
                    "config/Caddyfile", ".env", "a/.git/hooks/x.py", "bin/run.exe", "docker/Dockerfile"):
            r = self.run_v({bad: "x = 1\n"})
            self.assertIn("ruta", self.kinds(r), bad)

    def test_json_y_shell(self):
        self.assertIn("sintaxis", self.kinds(self.run_v({"a.json": "{malo"})))
        self.assertIn("sintaxis", self.kinds(self.run_v({"a.sh": "#!/bin/sh\nif then\n"})))
        self.assertEqual(self.run_v({"a.sh": "#!/bin/sh\necho hola\n", "a.json": "{}"}).errors, [])


class ParserTests(unittest.TestCase):
    def test_parse(self):
        raw = ("=== COMMIT ===\nfeat: agrega x\n\ncuerpo\n=== FILE: a/b.py ===\n```python\nprint(1)\n```\n=== END FILE ===\n"
               "=== FILE: c.json ===\n{}\n=== END FILE ===\n")
        p = engine.parse_response(raw)
        self.assertEqual(p.files["a/b.py"], "print(1)\n")
        self.assertEqual(p.files["c.json"], "{}\n")
        self.assertTrue(p.commit.startswith("feat: agrega x"))

    def test_razonamiento_se_ignora(self):
        raw = ("<think>=== FILE: falso.py ===\nx = (\n=== END FILE ===</think>\n"
               "<analisis>=== FILE: otro.py ===\ny\n=== END FILE ===</analisis>\n"
               "=== COMMIT ===\nfix: arregla\n=== FILE: real.py ===\nZ = 1\n=== END FILE ===\n")
        p = engine.parse_response(raw)
        self.assertEqual(list(p.files), ["real.py"])
        self.assertEqual(engine.parse_review("<think>hmm</think>\nRECHAZADO\n- a.py:1: x"), ["a.py:1: x"])

    def test_render_sin_razonamiento(self):
        p = engine.parse_response("<analisis>pienso</analisis>\n=== COMMIT ===\nfeat: a\n=== FILE: a.py ===\nA = 1\n=== END FILE ===\n")
        self.assertNotIn("pienso", engine.render(p))
        self.assertEqual(engine.parse_response(engine.render(p)).files, p.files)

    def test_mensaje_de_commit(self):
        rep = validators.Report(tools=["ast+imports"])
        msg = autogit.build_commit_message("", rep, "m", {"a.py": True})
        self.assertTrue(msg.startswith("feat: agrega a.py"))
        msg = autogit.build_commit_message("agrega validación", rep, "m", {"a.py": False, "b.py": False})
        self.assertTrue(msg.startswith("chore: agrega validación"))
        msg = autogit.build_commit_message("docs: guía\n\nexplica", rep, "m", {"README.md": False})
        self.assertTrue(msg.startswith("docs: guía\n\nexplica"))
        self.assertIn("- actualiza README.md", msg)

    def test_review(self):
        self.assertEqual(engine.parse_review("APROBADO"), [])
        self.assertEqual(engine.parse_review("RECHAZADO\n- a.py:3: falta manejar None\n- b.py:1: x"),
                         ["a.py:3: falta manejar None", "b.py:1: x"])


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        remote_dir = os.path.join(self.tmp, "remote", "acme")
        os.makedirs(remote_dir)
        self.bare = os.path.join(remote_dir, "demo.git")
        sh("git", "init", "-q", "--bare", "-b", "main", self.bare)
        seed = os.path.join(self.tmp, "seed")
        sh("git", "clone", "-q", self.bare, seed)
        with open(os.path.join(seed, "README.md"), "w") as fh:
            fh.write("# demo\n")
        sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A", cwd=seed)
        sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init", cwd=seed)
        sh("git", "push", "-q", "origin", "HEAD:main", cwd=seed)
        patches = {
            "WORKSPACE": os.path.join(self.tmp, "ws"),
            "REMOTE_TEMPLATE": "file://" + os.path.join(self.tmp, "remote", "{repo}.git"),
            "TARGET_BRANCH": "", "GITHUB_TOKEN": "", "API_TOKEN": "", "BASE_BRANCH": "", "PROTECTED_REPOS": ["acme/demo"],
        }
        for k, v in patches.items():
            p = mock.patch.object(config, k, v)
            p.start()
            self.addCleanup(p.stop)

    def make_llm(self, outputs, review_answers):
        calls = {"gen": 0, "rev": 0}

        def llm(messages, temperature=0.1):
            if messages[0]["content"].startswith("Eres un revisor"):
                ans = review_answers[min(calls["rev"], len(review_answers) - 1)]
                calls["rev"] += 1
                return ans
            ans = outputs[min(calls["gen"], len(outputs) - 1)]
            calls["gen"] += 1
            return ans
        llm.calls = calls
        return llm

    def test_ciclo_corrige_y_publica(self):
        malo = "=== COMMIT ===\nfeat: suma\n=== FILE: calc/suma.py ===\ndef suma(a, b)\n    return a + b\n=== END FILE ===\n"
        bueno = ("=== COMMIT ===\nfeat: agrega función suma\n\nSuma dos números.\n"
                 "=== FILE: calc/suma.py ===\ndef suma(a, b):\n    return a + b\n=== END FILE ===\n")
        llm = self.make_llm([malo, bueno], ["APROBADO"])
        task = {"id": "t1", "instruction": "crea una función suma", "repo": "acme/demo"}
        res = engine.run_task(task, Workspace("acme/demo"), llm=llm)
        self.assertEqual(res["status"], "done", res)
        self.assertEqual(res["iterations"], 2)
        branch = res["branch"]
        self.assertTrue(branch.startswith("agent/"))
        out = sh("git", "--git-dir", self.bare, "show", f"{branch}:calc/suma.py")
        self.assertIn("def suma(a, b):", out)
        msg = sh("git", "--git-dir", self.bare, "log", "-1", "--format=%B", branch)
        self.assertTrue(msg.startswith("feat: agrega función suma"))
        self.assertEqual(sh("git", "--git-dir", self.bare, "rev-parse", "main"),
                         sh("git", "--git-dir", self.bare, "rev-parse", f"{branch}~1"))

    def test_revisor_pide_correccion(self):
        v1 = "=== COMMIT ===\nfeat: div\n=== FILE: d.py ===\ndef d(a, b):\n    return a / b\n=== END FILE ===\n"
        v2 = ("=== COMMIT ===\nfeat: div seguro\n=== FILE: d.py ===\ndef d(a, b):\n    if b == 0:\n        raise ValueError('b=0')\n"
              "    return a / b\n=== END FILE ===\n")
        llm = self.make_llm([v1, v2], ["RECHAZADO\n- d.py:2: no valida división por cero", "APROBADO"])
        res = engine.run_task({"id": "t2", "instruction": "función de división", "repo": "acme/demo"},
                              Workspace("acme/demo"), llm=llm)
        self.assertEqual(res["status"], "done", res)
        self.assertEqual(res["iterations"], 2)
        self.assertEqual(res["review_notes"], [])

    def test_falla_si_nunca_valida(self):
        malo = "=== FILE: x.py ===\nimport paquete_que_no_existe\n=== END FILE ===\n"
        llm = self.make_llm([malo], ["APROBADO"])
        res = engine.run_task({"id": "t3", "instruction": "algo con un paquete", "repo": "acme/demo"},
                              Workspace("acme/demo"), llm=llm)
        self.assertEqual(res["status"], "failed")
        branches = sh("git", "--git-dir", self.bare, "branch", "--list", "agent/*")
        self.assertEqual(branches, "")

    def test_ruta_protegida_se_rechaza(self):
        salida = "=== FILE: config/Caddyfile ===\nx\n=== END FILE ===\n"
        llm = self.make_llm([salida], ["APROBADO"])
        res = engine.run_task({"id": "t4", "instruction": "cambia el Caddyfile", "repo": "acme/demo"},
                              Workspace("acme/demo"), llm=llm)
        self.assertEqual(res["status"], "failed")
        self.assertTrue(any("protegida" in e or "extensión" in e for e in res["errors"]))

    def test_dry_run_no_sube_nada(self):
        ok = "=== COMMIT ===\nfeat: x\n=== FILE: x.py ===\nX = 1\n=== END FILE ===\n"
        llm = self.make_llm([ok], ["APROBADO"])
        res = engine.run_task({"id": "t5", "instruction": "define una constante X", "repo": "acme/demo", "dry_run": True},
                              Workspace("acme/demo"), llm=llm)
        self.assertEqual(res["status"], "done")
        self.assertTrue(res["dry_run"])
        self.assertEqual(sh("git", "--git-dir", self.bare, "branch", "--list", "agent/*"), "")


    def _push_externo(self, rama, archivo, texto):
        other = os.path.join(self.tmp, "otro-" + rama)
        sh("git", "clone", "-q", self.bare, other)
        sh("git", "checkout", "-q", "-B", rama, f"origin/{rama}" if rama == "main" else "origin/main", cwd=other)
        with open(os.path.join(other, archivo), "w") as fh:
            fh.write(texto)
        ident = ("-c", "user.name=o", "-c", "user.email=o@o")
        sh("git", *ident, "add", "-A", cwd=other)
        sh("git", *ident, "commit", "-qm", "externo", cwd=other)
        sh("git", "push", "-q", "origin", f"HEAD:refs/heads/{rama}", cwd=other)

    def test_push_rechazado_reintenta(self):
        with mock.patch.object(config, "TARGET_BRANCH", "main"):
            ws = Workspace("acme/demo")
            ws.sync()
            self._push_externo("main", "externo.txt", "otro cambio\n")  # main avanza durante la tarea
            report = validators.validate_files({"nuevo.py": "X = 1\n"}, ws.root)
            info = autogit.publish(ws, report, "feat: agrega nuevo")
        self.assertTrue(info["pushed"])
        archivos = sh("git", "--git-dir", self.bare, "ls-tree", "-r", "--name-only", "main").split()
        self.assertIn("nuevo.py", archivos)
        self.assertIn("externo.txt", archivos)

    def test_rama_destino_existente(self):
        self._push_externo("develop", "dev.txt", "dev\n")
        with mock.patch.object(config, "TARGET_BRANCH", "develop"):
            llm = self.make_llm(["=== COMMIT ===\nfeat: x\n=== FILE: x.py ===\nX = 1\n=== END FILE ===\n"], ["APROBADO"])
            res = engine.run_task({"id": "t6", "instruction": "define la constante X", "repo": "acme/demo"},
                                  Workspace("acme/demo"), llm=llm)
        self.assertEqual(res["status"], "done", res)
        archivos = sh("git", "--git-dir", self.bare, "ls-tree", "-r", "--name-only", "develop").split()
        self.assertIn("x.py", archivos)
        self.assertIn("dev.txt", archivos)

    def test_cli_autogit(self):
        src = os.path.join(self.tmp, "src")
        os.makedirs(src)
        with open(os.path.join(src, "tool.py"), "w") as fh:
            fh.write("import json\nprint(json.dumps({}))\n")
        with open(os.path.join(src, "roto.py"), "w") as fh:
            fh.write("def f(:\n")
        with mock.patch.object(config, "ALLOWED_REPOS", ["acme/demo"]), mock.patch.object(config, "GITHUB_REPO", "acme/demo"):
            self.assertEqual(autogit.main(["--source", src, "roto.py"]), 1)
            self.assertEqual(autogit.main(["--source", src, "--dry-run", "tool.py"]), 0)
            self.assertEqual(sh("git", "--git-dir", self.bare, "branch", "--list", "agent/*"), "")
            self.assertEqual(autogit.main(["--source", src, "-m", "feat: agrega tool", "tool.py"]), 0)
            self.assertEqual(autogit.main(["--source", src, "--repo", "otro/repo", "tool.py"]), 2)
        self.assertNotEqual(sh("git", "--git-dir", self.bare, "branch", "--list", "agent/*"), "")


if __name__ == "__main__":
    unittest.main()
