FROM ollama/ollama:latest
ENV OLLAMA_HOST=0.0.0.0:11434
ENV OLLAMA_MODELS=/root/.ollama/models
ENV OLLAMA_KEEP_ALIVE=30m
COPY config/Modelfile /config/Modelfile
VOLUME ["/root/.ollama"]
EXPOSE 11434
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=5 \
  CMD ollama list > /dev/null 2>&1 || exit 1
ENTRYPOINT ["/bin/ollama"]
CMD ["serve"]
