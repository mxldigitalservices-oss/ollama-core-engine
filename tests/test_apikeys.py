"""Pruebas del módulo apikeys: python3 -m unittest tests.test_apikeys -v  (sin red, reloj simulado)."""
import base64
import hashlib
import hmac
import json
import os
import tempfile
import threading
import unittest

from apikeys import (JsonFileStore, KeyManager, NonceCache, SignatureError, SignatureProfile, Signer,
                     SigningKeyRing, TokenError, TokenInvalid, TokenIssuer, TokenManager, hash_secret,
                     PROFILE_BODY_DOT_TIMESTAMP, PROFILE_SORTED_PARAMS)
from apikeys.signing import Verifier, canonical_query

PEPPER = "pepper-de-prueba-0123456789"


class Clock:
    def __init__(self, now=1_700_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class KeyTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.mgr = KeyManager(PEPPER, clock=self.clock)

    def test_formato_y_unicidad(self):
        a, b = self.mgr.create("app"), self.mgr.create("app")
        self.assertTrue(a.token.startswith("sk_" + a.key_id + "_"))
        self.assertNotEqual(a.secret, b.secret)
        self.assertGreaterEqual(len(a.secret), 43)  # 32 bytes en base64url

    def test_solo_se_guarda_el_hash(self):
        key = self.mgr.create("app")
        record = self.mgr.store.get(key.key_id)
        self.assertEqual(record.secret_hash, hash_secret(key.secret, PEPPER))
        self.assertNotIn(key.secret, json.dumps(record.__dict__, default=str))
        self.assertNotEqual(hash_secret(key.secret, PEPPER), hash_secret(key.secret, PEPPER + "x"))

    def test_verificacion(self):
        key = self.mgr.create("app", scopes=("read",))
        self.assertEqual(self.mgr.verify(key.token).key_id, key.key_id)
        self.assertIsNotNone(self.mgr.verify(key.token, required_scope="read"))
        self.assertIsNone(self.mgr.verify(key.token, required_scope="write"))
        self.assertIsNone(self.mgr.verify(key.token[:-1] + ("A" if key.token[-1] != "A" else "B")))
        self.assertIsNone(self.mgr.verify("pk_" + key.token.split("_", 1)[1]))  # prefijo distinto
        for basura in ("", "sk", "sk_x_y", "a_b_c", None, 123, "sk_" + "0" * 12 + "_zzz"):
            self.assertIsNone(self.mgr.verify(basura))

    def test_ttl_y_revocacion(self):
        key = self.mgr.create("app", ttl=60)
        self.clock.advance(59)
        self.assertIsNotNone(self.mgr.verify(key.token))
        self.clock.advance(1)
        self.assertIsNone(self.mgr.verify(key.token))
        otra = self.mgr.create("app")
        self.assertTrue(self.mgr.revoke(otra.key_id))
        self.assertFalse(self.mgr.revoke(otra.key_id))
        self.assertIsNone(self.mgr.verify(otra.token))

    def test_rotacion_con_periodo_de_gracia(self):
        old = self.mgr.create("app", scopes=("read",))
        new = self.mgr.rotate(old.key_id, grace=100)
        self.assertEqual(self.mgr.store.get(old.key_id).replaced_by, new.key_id)
        self.assertEqual(self.mgr.verify(new.token).scopes, ("read",))
        self.clock.advance(99)
        self.assertIsNotNone(self.mgr.verify(old.token))
        self.clock.advance(1)
        self.assertIsNone(self.mgr.verify(old.token))
        self.assertIsNotNone(self.mgr.verify(new.token))
        with self.assertRaises(KeyError):
            self.mgr.rotate("0" * 12)

    def test_rotacion_no_alarga_un_vencimiento_previo(self):
        old = self.mgr.create("app", ttl=30)
        self.mgr.rotate(old.key_id, grace=1000)
        self.clock.advance(31)
        self.assertIsNone(self.mgr.verify(old.token))

    def test_validaciones_de_entrada(self):
        for pepper in ("", "corto"):
            with self.assertRaises(ValueError):
                KeyManager(pepper)
        for prefix in ("", "A", "con_guion", "1abc", "x" * 13):
            with self.assertRaises(ValueError):
                self.mgr.create("app", prefix=prefix)
        with self.assertRaises(ValueError):
            self.mgr.create("app", ttl=0)

    def test_almacen_json_persistente_y_purga(self):
        path = os.path.join(tempfile.mkdtemp(), "keys.json")
        mgr = KeyManager(PEPPER, JsonFileStore(path), clock=self.clock)
        key = mgr.create("app", ttl=10, scopes=("a", "b"))
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
        with open(path, encoding="utf-8") as fh:
            self.assertNotIn(key.secret, fh.read())
        again = KeyManager(PEPPER, JsonFileStore(path), clock=self.clock)
        self.assertEqual(again.verify(key.token).scopes, ("a", "b"))
        self.clock.advance(11)
        self.assertEqual(again.purge_expired(), 1)
        self.assertEqual(JsonFileStore(path).all(), [])


class SigningTests(unittest.TestCase):
    SECRET = "secreto-de-firma-123456"

    def setUp(self):
        self.clock = Clock()
        self.verifier = Verifier(lambda k: self.SECRET if k == "app1" else None, clock=self.clock)
        self.signer = Signer("app1", self.SECRET, clock=self.clock)

    def sign(self, **kw):
        kw.setdefault("body", b'{"a":1}')
        return self.signer.sign("post", "https://api.test/v1/orders?b=2&a=1", **kw)

    def check(self, signed, **kw):
        args = {"method": "POST", "url": "https://api.test/v1/orders?a=1&b=2", "body": b'{"a":1}'}
        args.update(kw)
        return self.verifier.verify(args.pop("method"), args.pop("url"), signed.headers, **args)

    def test_ida_y_vuelta_ignora_el_orden_de_query(self):
        self.assertEqual(self.check(self.sign()), "app1")

    def test_vector_independiente(self):
        s = self.sign(nonce="n1")
        body_hash = hashlib.sha256(b'{"a":1}').hexdigest()
        canonical = f"POST\n/v1/orders\na=1&b=2\n{body_hash}\n{int(self.clock())}\nn1"
        self.assertEqual(s.canonical, canonical)
        self.assertEqual(s.signature, hmac.new(self.SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest())

    def test_alteraciones_se_rechazan(self):
        s = self.sign()
        for kw in ({"body": b'{"a":2}'}, {"url": "https://api.test/v1/orders?a=1&b=3"},
                   {"url": "https://api.test/v1/admin?a=1&b=2"}, {"method": "PUT"}):
            with self.assertRaises(SignatureError) as ctx:
                self.check(s, **kw)
            self.assertEqual(ctx.exception.reason, "signature")

    def test_secreto_incorrecto_y_clave_desconocida(self):
        bad = Signer("app1", "otro-secreto-123456789", clock=self.clock).sign("POST", "https://api.test/x")
        with self.assertRaises(SignatureError):
            self.verifier.verify("POST", "https://api.test/x", bad.headers)
        ghost = Signer("fantasma", self.SECRET, clock=self.clock).sign("POST", "https://api.test/x")
        with self.assertRaises(SignatureError) as ctx:
            self.verifier.verify("POST", "https://api.test/x", ghost.headers)
        self.assertEqual(ctx.exception.reason, "unknown_key")

    def test_ventana_de_tiempo(self):
        s = self.sign()
        self.clock.advance(301)
        with self.assertRaises(SignatureError) as ctx:
            self.check(s)
        self.assertEqual(ctx.exception.reason, "timestamp")

    def test_replay(self):
        s = self.sign()
        self.check(s)
        with self.assertRaises(SignatureError) as ctx:
            self.check(s)
        self.assertEqual(ctx.exception.reason, "replay")

    def test_firma_invalida_no_consume_el_nonce(self):
        s = self.sign()
        with self.assertRaises(SignatureError):
            self.check(s, body=b"{}")
        self.assertEqual(self.check(s), "app1")

    def test_cabeceras_faltantes_y_mayusculas(self):
        s = self.sign()
        lower = {k.lower(): v for k, v in s.headers.items()}
        self.assertEqual(self.verifier.verify("POST", "https://api.test/v1/orders?a=1&b=2", lower, body=b'{"a":1}'), "app1")
        with self.assertRaises(SignatureError) as ctx:
            self.verifier.verify("POST", "https://api.test/x", {})
        self.assertEqual(ctx.exception.reason, "missing")

    def test_perfiles_incluidos(self):
        for profile, body in ((PROFILE_SORTED_PARAMS, '{"x":1}'), (PROFILE_BODY_DOT_TIMESTAMP, '{"x":1}')):
            signer = Signer("app1", self.SECRET, profile, clock=self.clock)
            verifier = Verifier(lambda k: self.SECRET, profile, clock=self.clock)
            s = signer.sign("POST", "https://api.test/v1/x?z=9", body=body)
            self.assertEqual(verifier.verify("POST", "https://api.test/v1/x?z=9", s.headers, body=body), "app1")
        s = Signer("app1", self.SECRET, PROFILE_BODY_DOT_TIMESTAMP, clock=self.clock).sign("POST", "/x", body='{"x":1}')
        esperado = hmac.new(self.SECRET.encode(), f'{int(self.clock())}.{{"x":1}}'.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(s.signature, esperado)  # contrato fijo: HMAC(secreto, "<ts>.<cuerpo>") en hex

    def test_perfil_personalizado_base64_y_milisegundos(self):
        profile = SignatureProfile("tercero", lambda p: f"{p.path}&{p.timestamp}&{p.body.decode()}",
                                   encoding="base64", timestamp_unit="ms", key_header="Appid",
                                   timestamp_header="Timestamp", nonce_header=None, signature_header="Signature")
        s = Signer("app1", self.SECRET, profile, clock=self.clock).sign("POST", "https://t.test/api/orders", body='{"k":1}')
        self.assertEqual(s.timestamp, str(int(self.clock() * 1000)))
        mac = hmac.new(self.SECRET.encode(), s.canonical.encode(), hashlib.sha256).digest()
        self.assertEqual(s.signature, base64.b64encode(mac).decode())
        verifier = Verifier(lambda k: self.SECRET, profile, clock=self.clock)
        self.assertEqual(verifier.verify("POST", "https://t.test/api/orders", s.headers, body='{"k":1}'), "app1")

    def test_entradas_invalidas_y_cache_de_nonces(self):
        with self.assertRaises(ValueError):
            Signer("", self.SECRET)
        with self.assertRaises(ValueError):
            Signer("app1", "corto")
        self.assertEqual(canonical_query([("b", "x y"), ("a", "1")]), "a=1&b=x%20y")
        cache = NonceCache(ttl=10, max_items=2, clock=self.clock)
        self.assertTrue(cache.add("a"))
        self.assertFalse(cache.add("a"))
        cache.add("b"), cache.add("c")  # desaloja "a" por el límite
        self.assertTrue(cache.add("a"))
        self.clock.advance(11)
        self.assertTrue(cache.add("b"))


class FakeServer:
    """Transporte simulado del endpoint /token."""

    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, url, data, headers, timeout):
        self.calls.append((url, dict(data), dict(headers)))
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item


def ok(token, expires=3600, refresh=None):
    body = {"access_token": token, "expires_in": expires, "token_type": "Bearer"}
    if refresh:
        body["refresh_token"] = refresh
    return 200, body


class OAuthTests(unittest.TestCase):
    URL = "https://auth.test/oauth/token"

    def manager(self, server, clock=None, **kw):
        self.clock = clock or Clock()
        self.sleeps = []
        return TokenManager(self.URL, "cid", "csecret", transport=server, clock=self.clock, sleep=self.sleeps.append, **kw)

    def test_client_credentials_y_cache(self):
        server = FakeServer(ok("t1"))
        mgr = self.manager(server)
        self.assertEqual(mgr.get_token(), "t1")
        self.assertEqual(mgr.get_token(), "t1")
        self.assertEqual(len(server.calls), 1)
        self.assertEqual(server.calls[0][1]["grant_type"], "client_credentials")
        expected = "Basic " + base64.b64encode(b"cid:csecret").decode()
        self.assertEqual(server.calls[0][2]["Authorization"], expected)
        self.assertEqual(mgr.authorization_header(), {"Authorization": "Bearer t1"})

    def test_refresco_automatico_antes_de_vencer(self):
        server = FakeServer(ok("t1", 300), ok("t2", 300))
        mgr = self.manager(server, skew=60)
        self.assertEqual(mgr.get_token(), "t1")
        self.clock.advance(239)
        self.assertEqual(mgr.get_token(), "t1")
        self.clock.advance(2)  # quedan 59 s < skew
        self.assertEqual(mgr.get_token(), "t2")
        self.assertEqual(len(server.calls), 2)

    def test_rotacion_del_refresh_token(self):
        server = FakeServer(ok("a1", 100, refresh="r2"), ok("a2", 100, refresh="r3"))
        saved = []
        mgr = self.manager(server, refresh_token="r1", on_update=saved.append, skew=10)
        mgr.get_token()
        self.assertEqual(server.calls[0][1]["refresh_token"], "r1")
        self.clock.advance(95)
        self.assertEqual(mgr.get_token(), "a2")
        self.assertEqual(server.calls[1][1]["refresh_token"], "r2")  # usa el rotado
        self.assertEqual(mgr.state()["refresh_token"], "r3")
        self.assertEqual([s["refresh_token"] for s in saved], ["r2", "r3"])

    def test_refresh_token_revocado_cae_a_client_credentials(self):
        server = FakeServer((400, {"error": "invalid_grant"}), ok("nuevo"))
        mgr = self.manager(server, refresh_token="viejo")
        self.assertEqual(mgr.get_token(), "nuevo")
        self.assertEqual([c[1]["grant_type"] for c in server.calls], ["refresh_token", "client_credentials"])
        self.assertIsNone(mgr.state()["refresh_token"])

    def test_reintentos_con_backoff(self):
        server = FakeServer((503, {}), OSError("caída"), ok("t1"))
        mgr = self.manager(server, retries=2, backoff=0.5)
        self.assertEqual(mgr.get_token(), "t1")
        self.assertEqual(self.sleeps, [0.5, 1.0])
        agotado = self.manager(FakeServer((503, {})), retries=1)
        with self.assertRaises(TokenError):
            agotado.get_token()

    def test_credenciales_rechazadas_no_filtran_secretos(self):
        mgr = self.manager(FakeServer((401, {"error": "invalid_client", "error_description": "csecret"})))
        with self.assertRaises(TokenError) as ctx:
            mgr.get_token()
        self.assertIn("invalid_client", str(ctx.exception))
        self.assertNotIn("csecret", str(ctx.exception))

    def test_respuestas_defectuosas(self):
        with self.assertRaises(TokenError):
            self.manager(FakeServer((200, {"expires_in": 5}))).get_token()
        mgr = self.manager(FakeServer((200, {"access_token": "x", "expires_in": "basura"})), default_ttl=120)
        mgr.get_token()
        self.assertEqual(mgr.seconds_left(), 120)
        sin_secreto = TokenManager(self.URL, "cid", transport=FakeServer(ok("x")))
        with self.assertRaises(TokenError):
            sin_secreto.get_token()

    def test_force_refresh_tras_401(self):
        server = FakeServer(ok("t1"), ok("t2"))
        mgr = self.manager(server)
        mgr.get_token()
        self.assertEqual(mgr.force_refresh(), "t2")

    def test_estilo_body_y_scope(self):
        server = FakeServer(ok("t1"))
        self.manager(server, auth_style="body", scope="read write").get_token()
        data, headers = server.calls[0][1], server.calls[0][2]
        self.assertEqual((data["client_id"], data["client_secret"], data["scope"]), ("cid", "csecret", "read write"))
        self.assertNotIn("Authorization", headers)

    def test_solo_https(self):
        with self.assertRaises(ValueError):
            TokenManager("http://auth.test/token", "cid", "s")
        TokenManager("http://127.0.0.1:9000/token", "cid", "s")
        with self.assertRaises(ValueError):
            TokenManager(self.URL, "cid", "s", auth_style="otro")

    def test_hilos_concurrentes_hacen_una_sola_peticion(self):
        server = FakeServer(ok("t1"))
        mgr = self.manager(server)
        results = []
        threads = [threading.Thread(target=lambda: results.append(mgr.get_token())) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results, ["t1"] * 8)
        self.assertEqual(len(server.calls), 1)


class BearerTokenTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.ring = SigningKeyRing(clock=self.clock)
        self.ring.rotate()
        self.issuer = TokenIssuer(self.ring, ttl=600, max_lifetime=3600, issuer="mi-api", clock=self.clock)

    def test_emision_y_verificacion(self):
        token = self.issuer.issue("cliente-1", scopes=("orders:read",))
        claims = self.issuer.verify(token, required_scope="orders:read")
        self.assertEqual((claims["sub"], claims["iss"], claims["exp"] - claims["iat"]), ("cliente-1", "mi-api", 600))
        with self.assertRaises(TokenInvalid) as ctx:
            self.issuer.verify(token, required_scope="orders:write")
        self.assertEqual(ctx.exception.reason, "scope")

    def test_expiracion_ttl(self):
        token = self.issuer.issue("c", ttl=30)
        self.clock.advance(29)
        self.issuer.verify(token)
        self.clock.advance(1)
        with self.assertRaises(TokenInvalid) as ctx:
            self.issuer.verify(token)
        self.assertEqual(ctx.exception.reason, "expired")

    def test_alteraciones_y_basura(self):
        token = self.issuer.issue("c")
        version, kid, payload, sig = token.split(".")
        forged = json.dumps({"sub": "admin", "scp": [], "exp": 9_999_999_999}).encode()
        forged_payload = base64.urlsafe_b64encode(forged).rstrip(b"=").decode()
        casos = {".".join([version, kid, forged_payload, sig]): "bad_signature",
                 ".".join([version, "ffffffff", payload, sig]): "unknown_kid"}
        for bad, reason in casos.items():
            with self.assertRaises(TokenInvalid) as ctx:
                self.issuer.verify(bad)
            self.assertEqual(ctx.exception.reason, reason)
        for basura in ("", "a.b", "v2.a.b.c", "v1.x.y.z", None, 5):
            with self.assertRaises(TokenInvalid):
                self.issuer.verify(basura)

    def test_rotacion_de_llaves_con_gracia(self):
        old_token = self.issuer.issue("c", ttl=3000)
        old_kid = old_token.split(".")[1]
        self.ring.rotate(grace=100)
        new_token = self.issuer.issue("c")
        self.assertNotEqual(new_token.split(".")[1], old_kid)
        self.clock.advance(99)
        self.issuer.verify(old_token)  # aún en gracia
        self.clock.advance(1)
        with self.assertRaises(TokenInvalid) as ctx:
            self.issuer.verify(old_token)
        self.assertEqual(ctx.exception.reason, "unknown_kid")
        self.issuer.verify(new_token)
        self.assertEqual(self.ring.purge(), 1)

    def test_refresh_con_tope_absoluto(self):
        token = self.issuer.issue("c", scopes=("a",))
        self.clock.advance(500)
        renewed = self.issuer.refresh(token)
        self.assertEqual(self.issuer.verify(renewed)["scp"], ["a"])
        for _ in range(5):
            self.clock.advance(590)
            renewed = self.issuer.refresh(renewed)
        self.assertLessEqual(self.issuer.verify(renewed)["exp"], int(1_700_000_000 + 3600))  # nunca pasa max_lifetime
        self.clock.advance(590)
        with self.assertRaises(TokenInvalid):  # el tope lo vence
            self.issuer.refresh(renewed)

    def test_no_se_refresca_un_token_vencido(self):
        token = self.issuer.issue("c", ttl=10)
        self.clock.advance(11)
        with self.assertRaises(TokenInvalid):
            self.issuer.refresh(token)

    def test_validaciones_de_configuracion(self):
        with self.assertRaises(ValueError):
            self.ring.add_key("k", b"corta")
        with self.assertRaises(ValueError):
            self.ring.add_key("k.x", b"x" * 32)
        with self.assertRaises(RuntimeError):
            TokenIssuer(SigningKeyRing()).issue("c")
        with self.assertRaises(ValueError):
            TokenIssuer(self.ring, ttl=100, max_lifetime=10)


if __name__ == "__main__":
    unittest.main()
