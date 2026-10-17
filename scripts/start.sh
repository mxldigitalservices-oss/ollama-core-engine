#!/bin/sh
set -e
export OLLAMA_HOST="0.0.0.0:${PORT:-11434}"
ollama serve &
SERVER_PID=$!
until ollama list >/dev/null 2>&1; do sleep 1; done
ollama pull llama3.2
ollama create elite-architect -f /config/Modelfile
echo "Modelo elite-architect listo"
wait $SERVER_PID
