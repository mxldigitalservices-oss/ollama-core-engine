#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE_NAME="${IMAGE_NAME:-ollama-core-engine:local}"
CONTAINER_NAME="${CONTAINER_NAME:-ollama-core-engine}"
VOLUME_NAME="${VOLUME_NAME:-ollama_data}"
HOST_PORT="${HOST_PORT:-8080}"
API_TOKEN="${API_TOKEN:-$(openssl rand -hex 32)}"

command -v docker >/dev/null 2>&1 || { echo "Docker no está instalado."; exit 1; }
command -v curl >/dev/null 2>&1 || { echo "curl no está instalado."; exit 1; }
docker info >/dev/null 2>&1 || { echo "El daemon de Docker no está en ejecución."; exit 1; }

echo "Construyendo imagen ${IMAGE_NAME}..."
docker build -f "${ROOT_DIR}/docker/Dockerfile" -t "${IMAGE_NAME}" "${ROOT_DIR}"

docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true

echo "Levantando contenedor en el puerto ${HOST_PORT}..."
docker run -d \
  --name "${CONTAINER_NAME}" \
  --restart unless-stopped \
  -p "${HOST_PORT}:8080" \
  -e PORT=8080 \
  -e API_TOKEN="${API_TOKEN}" \
  -v "${VOLUME_NAME}:/root/.ollama" \
  "${IMAGE_NAME}" >/dev/null

echo "Esperando al proxy..."
for _ in $(seq 1 60); do
  if curl -fsS "http://localhost:${HOST_PORT}/health" >/dev/null 2>&1; then
    echo "Servicio activo en http://localhost:${HOST_PORT}"
    echo "API_TOKEN=${API_TOKEN}"
    echo "El modelo se descarga en segundo plano: docker logs -f ${CONTAINER_NAME}"
    exit 0
  fi
  sleep 2
done

docker logs "${CONTAINER_NAME}" || true
echo "El servicio no respondió a tiempo."
exit 1
