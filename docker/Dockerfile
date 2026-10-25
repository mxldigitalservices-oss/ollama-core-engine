FROM caddy:2 AS caddy

FROM ollama/ollama:latest
COPY --from=caddy /usr/bin/caddy /usr/bin/caddy

# Agente programador: Python (solo biblioteca estándar), pyflakes y git (~60 MB extra)
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-pyflakes git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ENV OLLAMA_HOST=127.0.0.1:11434
ENV OLLAMA_MODELS=/root/.ollama/models
ENV OLLAMA_KEEP_ALIVE=30m
ENV OLLAMA_NUM_PARALLEL=1
ENV OLLAMA_MAX_LOADED_MODELS=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
COPY config/Modelfile /config/Modelfile
COPY config/Caddyfile /etc/caddy/Caddyfile
COPY agent/ /opt/agent/
COPY apikeys/ /opt/apikeys/
COPY scripts/start.sh /start.sh
RUN chmod +x /start.sh
EXPOSE 8080
ENTRYPOINT ["/bin/sh", "/start.sh"]
