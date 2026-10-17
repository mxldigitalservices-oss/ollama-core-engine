FROM ollama/ollama:latest
ENV OLLAMA_HOST=0.0.0.0:11434
ENV OLLAMA_MODELS=/root/.ollama/models
ENV OLLAMA_KEEP_ALIVE=30m
COPY config/Modelfile /config/Modelfile
COPY scripts/start.sh /start.sh
RUN chmod +x /start.sh
EXPOSE 11434
ENTRYPOINT ["/bin/sh", "/start.sh"]
