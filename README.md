# ollama-core-engine

Agente programador autónomo **full-stack** sobre Ollama. Recibe un requerimiento abierto ("una web completa para un cliente, con interfaz, backend y base de datos"), razona la arquitectura paso a paso, genera el proyecto, lo **valida** y lo publica en GitHub con `commit` + `push`. Incluye un **dashboard web** (`/ui`) para hablar con el agente desde el navegador. Todo corre en un solo servicio de Railway, protegido con token Bearer mediante Caddy.

## Arquitectura

    navegador --> Caddy :PORT --+--> /ui, /ui/*   Dashboard (estático, sin secretos, sin token)
                                +--> /agent/*     Agente 127.0.0.1:8081 --> GitHub   (Bearer)
                                \--> resto        Ollama 127.0.0.1:11434             (Bearer)

- Caddy escucha en `PORT` (8080 por defecto). Todo exige `Authorization: Bearer <API_TOKEN>` **excepto** `GET /health` (healthcheck de Railway) y el dashboard `/ui`, que es HTML/JS estático sin datos: los datos viajan por `/agent/*` con el token que escribes en el login.
- `GET /` redirige a `/ui`. Ollama escucha solo en localhost dentro del contenedor.

## Estructura

    agent/engine.py         Ciclo de una tarea: generar -> validar -> revisar -> corregir -> publicar
    agent/project.py        Modo proyecto: plan -> lotes -> empaquetado -> validación -> publicación
    agent/validators.py     Validación estática (Python, JS/TS, CSS, HTML/EJS, Dockerfile, YAML, JSON, XML, secretos)
    agent/autogit.py        add + commit descriptivo + push (también como CLI)
    agent/gitops.py         Git seguro: token por GIT_ASKPASS, repos vacíos, verificación del push
    agent/server.py         API + dashboard (solo biblioteca estándar, sin dependencias extra)
    agent/ui/               Dashboard: index.html (Tailwind) + app.js
    apikeys/                Módulo de API keys, firmas HMAC y tokens OAuth2/Bearer (stdlib)
    tests/                  Pruebas (python3 -m unittest discover -s tests -v)
    config/Modelfile        Modelo y System Prompt full-stack
    config/Caddyfile        Proxy con autenticación
    docker/Dockerfile       Imagen: Ollama + Caddy + agente
    scripts/start.sh        Arranque: proxy, agente, descarga de modelos
    scripts/setup.sh        Despliegue local con Docker
    railway.json            Configuración de Railway

## Dashboard: cómo conectarte

1. Abre `https://TU-DOMINIO.up.railway.app/ui`.
2. Pega tu `API_TOKEN` (queda en `sessionStorage`; marca "Recordar" para `localStorage`).
3. Elige **modo** (Tarea o Proyecto completo), **modelo** y **repositorio destino**, escribe el requerimiento y pulsa *Enviar al agente*.
4. Cada respuesta muestra el pipeline *Sincronizar → Razonar → Validar → Revisar → Commit*. En el panel lateral: **Razonamiento** (cadena de pensamiento y bitácora), **Validación** (errores, advertencias y observaciones del revisor antes del commit), **Archivos** (con el código completo si marcaste *Solo validar*) y **Conexión** (checklist de variables).

El dashboard carga Tailwind desde `cdn.tailwindcss.com` (versión fijada 3.4.17); necesita conexión a internet en tu navegador.

## Variables de entorno (Railway)

| Variable | ¿Obligatoria? | Para qué sirve |
|---|---|---|
| `API_TOKEN` | **Sí** | Token Bearer de Caddy, de la API y del login del dashboard. Genera uno con `openssl rand -hex 32` |
| `GITHUB_TOKEN` | **Sí** | Token fino de GitHub: *Repository access* solo a los repos destino, permiso *Contents: Read and write* |
| `GITHUB_REPO` | **Sí** | Repo destino por defecto (`owner/nombre`) |
| `PORT` | Recomendada | `8080` (puerto del dominio público) |
| `APIKEYS_PEPPER` | Solo si usas `apikeys` | Pepper del hash de API keys (mín. 16 caracteres) |
| `AGENT_ALLOWED_REPOS` | No | Repos que aparecen en el selector, separados por comas. Por defecto solo `GITHUB_REPO` |
| `BASE_MODEL` | No | Modelo base de `elite-architect` (por defecto `qwen2.5-coder:7b`) |
| `EXTRA_MODELS` | No | Modelos adicionales para el selector, p. ej. `deepseek-coder-v2:16b,qwen2.5-coder:14b` (se descargan en segundo plano) |
| `OLLAMA_BASE_URL` | No | Solo si Ollama corre en **otro** servicio. Por defecto `http://127.0.0.1:11434` |
| `AGENT_TARGET_BRANCH` | No | Vacío = rama nueva `agent/<fecha>-<slug>` por tarea (recomendado). `main` = push directo |
| `AGENT_PROTECTED_REPOS` | No | Repos "propios" con todas las rutas protegidas. Por defecto `GITHUB_REPO` (ver *Barreras*) |
| `AGENT_PROJECT_MAX_FILES`, `AGENT_PROJECT_BATCH`, `AGENT_MAX_ITERATIONS`, `AGENT_NUM_CTX`, `AGENT_NUM_PREDICT`, `AGENT_LLM_REVIEW`, `AGENT_ENABLED` | No | Ajustes finos (ver `agent/config.py`) |

Además: volumen montado en `/root/.ollama` (modelos y workspace) y memoria acorde al modelo. Sin `GITHUB_TOKEN` o `GITHUB_REPO` el agente y el dashboard no se activan. No agregues URLs de PostgreSQL, MySQL, Redis ni MongoDB a este servicio.

## Módulo `apikeys` (API keys, firmas y tokens)

Solo biblioteca estándar. Vive fuera de `agent/` a propósito: esa carpeta está protegida contra escritura del agente.

| Archivo | Qué hace |
|---|---|
| `keys.py` | Genera claves con `secrets` (`<prefijo>_<id>_<secreto>`, 256 bits). Guarda **solo** `HMAC-SHA256(pepper, secreto)`. TTL, alcances, revocación y rotación con periodo de gracia. Almacén en memoria o JSON (0600) |
| `signing.py` | Firma y verifica peticiones con HMAC-SHA256 mediante **perfiles configurables**; ventana de tiempo y anti-replay con nonces |
| `oauth.py` | `TokenManager`: `client_credentials` y `refresh_token`, refresco automático antes de vencer, rotación del refresh token, reintentos con backoff, un solo refresco aunque haya varios hilos |
| `tokens.py` | Tokens Bearer propios firmados con TTL, refresco con tope absoluto (`max_lifetime`) y rotación de llaves de firma con periodo de gracia |

    import os
    from apikeys import KeyManager, JsonFileStore, Signer, TokenManager

    keys = KeyManager(os.environ["APIKEYS_PEPPER"], JsonFileStore("/root/.ollama/apikeys.json"))
    key = keys.create("app-cliente", prefix="sk", ttl=86400 * 90, scopes=("orders:read",))
    print(key.token)                       # se muestra UNA vez
    keys.verify(token, required_scope="orders:read")   # KeyRecord o None
    keys.rotate(key.key_id, grace=3600)    # la anterior vale 1 h más

    signer = Signer(key_id, secret)        # perfil estándar; ver signing.py para otros
    signed = signer.sign("POST", "https://api.tercero.test/v1/orders", body=raw_json_bytes)
    # requests.post(url, data=raw_json_bytes, headers=signed.headers)

    oauth = TokenManager("https://auth.tercero.test/oauth/token", client_id, client_secret)
    headers = oauth.authorization_header()  # siempre vigente; tras un 401: oauth.force_refresh()

CLI: `cd /opt && APIKEYS_PEPPER=... python3 -m apikeys generate --name app --store /root/.ollama/apikeys.json --ttl 7776000`.

**Variable nueva:** `APIKEYS_PEPPER` (mínimo 16 caracteres; `openssl rand -hex 32`). Guárdala en Railway y respáldala: si la pierdes, las claves emitidas dejan de verificarse. No la pongas en el mismo almacén que los hashes.

**Firmas para DiDi y otros terceros:** no existe un esquema de firma universal (texto canónico, hex o base64, cabeceras y unidad de tiempo cambian por proveedor). No encontré una especificación pública de DiDi, por lo que **no está incluido un perfil "DiDi"**. Con la documentación de su portal de partners se define un `SignatureProfile` con sus reglas exactas (hay un ejemplo en `tests/test_apikeys.py::test_perfil_personalizado_base64_y_milisegundos`). Firma siempre los bytes exactos que enviarás.

### Qué ejecuta los tests

El validador del agente es **estático y nunca ejecuta código** (barrera de seguridad). Los tests los ejecuta: `python3 -m unittest discover -s tests -v` en tu máquina, y el workflow `.github/workflows/tests.yml` en cada push. Ese workflow está en `.github/`, ruta que el agente no puede escribir: se sube a mano una vez.

## Despliegue en Railway

1. Conecta el repositorio al servicio.
2. Variables: `API_TOKEN`, `GITHUB_TOKEN` y `GITHUB_REPO` (obligatorias), `PORT=8080` y las opcionales de la tabla anterior.
3. Networking: dominio público apuntando al puerto 8080.
4. Volumen montado en `/root/.ollama` para no volver a descargar el modelo.
5. Espera `Modelo elite-architect listo` en los logs y abre `/ui`.

## Uso

    curl -s https://TU-DOMINIO.up.railway.app/api/chat \
      -H "Authorization: Bearer $API_TOKEN" \
      -d '{"model":"elite-architect","messages":[{"role":"user","content":"Hola"}],"stream":false}'

Sin token la respuesta es `401 unauthorized`.

## Uso local

    ./scripts/setup.sh

Imprime el token generado. Para fijar uno propio: `API_TOKEN=... ./scripts/setup.sh`.

## Modelo base

`config/Modelfile` es una plantilla: `start.sh` sustituye su línea `FROM` por `BASE_MODEL` y ejecuta `ollama create elite-architect`. Parámetros: `temperature 0.2`, `top_p 0.9`, `num_ctx 16384`. El System Prompt obliga a razonar paso a paso en un bloque `<analisis>` (requisito, errores probables, verificación de imports y sintaxis) antes de entregar el código.

| `BASE_MODEL` | RAM aprox. con 16k de contexto | Nota |
|---|---|---|
| `qwen2.5-coder:7b` (defecto) | 8 GB | Equilibrio calidad/costo |
| `qwen2.5-coder:14b` | 14-16 GB | Mejor código, más lento en CPU |
| `deepseek-coder-v2:16b` | 14-16 GB | Alternativa; su razonamiento `<think>` se ignora al parsear (y se muestra en el panel) |

Kimi no está disponible como modelo local ligero en Ollama. Dimensiona la memoria del servicio en Railway antes de subir de tamaño y deja espacio en el volumen (el modelo se descarga una vez). Si desplegaste antes con `llama3.2`, puedes liberar espacio con `ollama rm llama3.2`.

## Personalizar el modelo

Edita el bloque `SYSTEM` de `config/Modelfile`, haz commit y push. Cada arranque ejecuta `ollama create` con el Modelfile actual.

## Agente programador autovalidado

### Dos modos

- **Tarea** (por defecto): genera o modifica hasta `AGENT_MAX_FILES` (10) archivos en una sola vuelta. Ideal para scripts, endpoints, correcciones.
- **Proyecto completo** (`"mode":"project"`): para requerimientos abiertos. El agente (1) razona y devuelve un **plan** de arquitectura (stack, archivos, variables de entorno); (2) genera los archivos **por lotes** de `AGENT_PROJECT_BATCH` (4) con el contexto de lo ya escrito; (3) **empaqueta**: agrega `README.md`, `.gitignore` y `.env.example` si faltan; (4) valida, revisa y corrige solo los archivos con problemas; (5) publica. Hasta 30 archivos (`AGENT_PROJECT_MAX_FILES`). Funciona también en un repo **vacío** recién creado: el primer commit crea la rama `main`.

Especialidades del prompt: frontend (HTML5, CSS3, JS/TS, React, EJS, Tailwind, PWA), backend (FastAPI, Flask, Express, REST), bases de datos (PostgreSQL, MongoDB, Redis, MySQL), DevOps (Docker, Caddy, Bash, Railway/Render) y automatización (scraping, archivos, XML, PDF).

### Flujo

1. Sincroniza el repositorio destino en `/root/.ollama/agent-workspace`.
2. El modelo razona en un bloque `<analisis>` (se muestra en el dashboard) y devuelve archivos completos + mensaje de commit.
3. **Validación estática** (obligatoria, no ejecuta el código): rutas seguras, sintaxis (`ast`, `sh -n`, JSON/TOML/XML/SVG; llaves y paréntesis en JS/TS/CSS y en `<script>` de HTML; HTML sin cerrar; Dockerfile con `FROM`; YAML sin tabuladores), imports resolubles (stdlib, módulos del repo o `requirements.txt` en cualquier carpeta), `pyflakes` y búsqueda de secretos.
4. **Revisión lógica por LLM** (segundo pase estricto, temperatura 0). Es orientativa: con modelos pequeños puede fallar. Con observaciones, vuelve a generar.
5. Hasta `AGENT_MAX_ITERATIONS` vueltas (3). Si la validación estática no pasa, **no se escribe ni se sube nada**.
6. `agent/autogit.py`: `git add` (solo lo tocado) → `commit` descriptivo (tipo, resumen, cambios y validaciones) → `git push` a `agent/<fecha>-<slug>` (o `main` en repo vacío). Reintenta una vez si el push es rechazado y, tras el push, **verifica con `ls-remote`** que GitHub recibió el commit.

### API

| Método y ruta | Descripción |
|---|---|
| `POST /agent/task` | Encola una tarea (202 + `id`). Campos: `instruction`, `mode` (`task`\|`project`), `model`, `repo`, `dry_run`, `paths`, `context_files` |
| `GET /agent/task/<id>` | Estado, `steps`, `reasoning`, `validation`, `plan`, `result` |
| `GET /agent/tasks` | Últimas tareas |
| `GET /agent/config` | Modelos, repos, límites y checklist de variables (solo si están definidas, nunca el valor) |
| `GET /agent/models` | Modelos instalados y sugeridos |

    curl -s https://TU-DOMINIO.up.railway.app/agent/task \
      -H "Authorization: Bearer $API_TOKEN" \
      -d '{"instruction":"Créame una API REST de clientes en FastAPI con PostgreSQL y Dockerfile","mode":"project","model":"qwen2.5-coder:7b","repo":"owner/cliente"}'

El `model` debe estar instalado en Ollama (`BASE_MODEL`/`EXTRA_MODELS`); `repo` debe estar en `AGENT_ALLOWED_REPOS`.

### Barreras de seguridad

- Nunca ejecuta el código generado; solo lo analiza.
- **Siempre** protegidos: `.git/`, `.github/` y `.env*` (se permite `.env.example`). En el repo propio del motor (`AGENT_PROTECTED_REPOS`, por defecto `GITHUB_REPO`) también `agent/`, `Caddyfile`, `Dockerfile`, `start.sh` y `railway.json`. En repos de clientes sí puede crear `Dockerfile`, `railway.json`, `Caddyfile`, etc.
- Recomendado: usa `GITHUB_REPO` para el motor y agrega los repos de clientes a `AGENT_ALLOWED_REPOS`; el token de GitHub debe tener acceso solo a los repos necesarios.
- El token de GitHub se pasa por `GIT_ASKPASS` (no aparece en URLs ni logs). `/agent/*` exige el Bearer en Caddy y otra vez en el servicio.
- El dashboard pinta todo con `textContent` (nunca `innerHTML`) y se sirve con CSP, `nosniff` y sin `frame`.
- Una tarea a la vez (cola); tamaño y número de archivos limitados.

### Publicar desde la terminal (autogit)

Con la misma validación, sin pasar por el modelo (por ejemplo, desde la consola de Railway):

    cd /opt && python3 -m agent.autogit --source /ruta/archivos -m "feat: agrega backup" scripts/backup_pg.py
    python3 -m agent.autogit --dry-run scripts/backup_pg.py   # solo valida

### Pruebas

    python3 -m unittest discover -s tests -v

## Notas

- La revisión del LLM y la validación estática no prueban que el proyecto funcione: ejecútalo y pruébalo antes de entregarlo a un cliente.
- Los modelos pequeños pueden inventar datos legales o fiscales. Verifica siempre contra fuentes oficiales (DGII) y ejecuta el código antes de usarlo.
- Para rotar el token, cambia `API_TOKEN` en Railway y redespliega.
