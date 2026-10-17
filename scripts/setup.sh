#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE_NAME="${IMAGE_NAME:-ollama-custom:latest}"
CONTAINER_NAME="${CONTAINER_NAME:-ollama-server}"
VOLUME_NAME="${VOLUME_NAME:-ollama_data}"
HOST_PORT="${HOST_PORT:-11434}"
CUSTOM_MODEL="${CUSTOM_MODEL:-custom-assistant}"
MODELFILE="${ROOT_DIR}/config/Modelfile"
LOG_FILE="${ROOT_DIR}/ollama-pull.log"
FOREGROUND=false

if [[ "${1:-}" == "--foreground" ]]; then
  FOREGROUND=true
fi

log() { printf '[%s] %s\n' "$(date +\%H:\%M:\%S)" "$*"; }
fail() { log "ERROR: $*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || fail "Docker no está instalado."
command -v curl >/dev/null 2>&1 || fail "curl no está instalado."
docker info >/dev/null 2>&1 || fail "El daemon de Docker no está en ejecución."
[[ -f "${MODELFILE}" ]] || fail "No se encontró ${MODELFILE}."

BASE_MODEL="$(awk 'toupper($1)=="FROM"{print $2; exit}' "${MODELFILE}")"
[[ -n "${BASE_MODEL}" ]] || fail "El Modelfile no define una instrucción FROM."

if grep -q '\[INJECT_PROMPT_HERE\]' "${MODELFILE}"; then
  log "AVISO: el Modelfile aún contiene el marcador [INJECT_PROMPT_HERE]."
fi

log "Construyendo imagen ${IMAGE_NAME}..."
docker build -f "${ROOT_DIR}/docker/Dockerfile" -t "${IMAGE_NAME}" "${ROOT_DIR}"

if docker ps -a --format '{{.Names}}' | grep -qx "${CONTAINER_NAME}"; then
  log "Eliminando contenedor previo ${CONTAINER_NAME}..."
  docker rm -f "${CONTAINER_NAME}" >/dev/null
fi

log "Levantando contenedor ${CONTAINER_NAME} en el puerto${HOST_PORT}..."
docker run -d \
  --name "${CONTAINER_NAME}" \
  --restart unless-stopped \
  -p "${HOST_PORT}:11434" \
  -v "${VOLUME_NAME}:/root/.ollama" \
  "${IMAGE_NAME}" >/dev/null

log "Verificando el servicio de Ollama..."
READY=false
for _ in $(seq 1 60); do
  if curl -fsS "http://localhost:${HOST_PORT}/api/version" >/dev/null 2>&1; then
    READY=true
    break
  fi
  sleep 2
done

if [[ "${READY}" != "true" ]]; then
  docker logs "${CONTAINER_NAME}" || true
  fail "Ollama no respondió en el tiempo esperado."
fi

log "Servicio activo: $(curl -fsS "http://localhost:${HOST_PORT}/api/version")"

provision_model() {
  log "Descargando modelo base ${BASE_MODEL}..."
  docker exec "${CONTAINER_NAME}" ollama pull "${BASE_MODEL}"
  log "Creando modelo ${CUSTOM_MODEL} desde el Modelfile..."
  docker exec "${CONTAINER_NAME}" ollama create "${CUSTOM_MODEL}" -f /config/Modelfile
  log "Modelo ${CUSTOM_MODEL} listo."
}

if [[ "${FOREGROUND}" == "true" ]]; then
  provision_model
else
  ( trap '' HUP; provision_model ) >"${LOG_FILE}" 2>&1 &
  BG_PID=$!
  disown "${BG_PID}"
  log "Descarga en segundo plano (PID ${BG_PID}). Progreso: tail -f ${LOG_FILE}"
fi

log "API disponible en http://localhost:${HOST_PORT}"
