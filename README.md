# ollama-core-engine

Servidor Ollama con el modelo `elite-architect` (base `llama3.2`), protegido con token Bearer mediante Caddy y desplegado en Railway.

## Arquitectura

    cliente --(Bearer token)--> Caddy :PORT --> Ollama 127.0.0.1:11434

- Caddy escucha en `PORT` (8080 por defecto) y exige `Authorization: Bearer <API_TOKEN>`.
- `GET /health` responde 200 sin token (healthcheck de Railway).
- Ollama escucha solo en localhost dentro del contenedor.

## Estructura

    config/Modelfile        Modelo y System Prompt
    config/Caddyfile        Proxy con autenticación
    docker/Dockerfile       Imagen: Ollama + Caddy
    scripts/start.sh        Arranque: proxy, descarga del modelo, create
    scripts/setup.sh        Despliegue local con Docker
    railway.json            Configuración de Railway

## Despliegue en Railway

1. Conecta el repositorio al servicio.
2. Variables: `API_TOKEN` (obligatoria, genérala con `openssl rand -hex 32`) y `PORT=8080`.
3. Networking: dominio público apuntando al puerto 8080.
4. Volumen montado en `/root/.ollama` para no volver a descargar el modelo.
5. Espera `Modelo elite-architect listo` en los logs.

## Uso

    curl -s https://TU-DOMINIO.up.railway.app/api/chat \
      -H "Authorization: Bearer $API_TOKEN" \
      -d '{"model":"elite-architect","messages":[{"role":"user","content":"Hola"}],"stream":false}'

Sin token la respuesta es `401 unauthorized`.

## Uso local

    ./scripts/setup.sh

Imprime el token generado. Para fijar uno propio: `API_TOKEN=... ./scripts/setup.sh`.

## Personalizar el modelo

Edita el bloque `SYSTEM` de `config/Modelfile`, haz commit y push. Cada arranque ejecuta `ollama create` con el Modelfile actual.

## Notas

- Los modelos de 3B pueden inventar datos legales o fiscales. Verifica siempre contra fuentes oficiales (DGII) y ejecuta el código antes de usarlo.
- Para rotar el token, cambia `API_TOKEN` en Railway y redespliega.
