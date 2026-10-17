FROM caddy:2 AS caddy

FROM ollama/ollama:latest
COPY --from=caddy /usr/bin/caddy /usr/bin/caddy
ENV OLLAMA_HOST=127.0.0.1:11434
ENV OLLAMA_MODELS=/root/.ollama/models
ENV OLLAMA_KEEP_ALIVE=30m
COPY config/Modelfile /config/Modelfile
COPY config/Caddyfile /etc/caddy/Caddyfile
COPY scripts/start.sh /start.sh
RUN chmod +x /start.sh
EXPOSE 8080
ENTRYPOINT ["/bin/sh", "/start.sh"]
