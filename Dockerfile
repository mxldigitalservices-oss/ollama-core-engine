FROM ollama/ollama:latest
ENV OLLAMA_HOST=0.0.0.0:11434
ENV OLLAMA_MODELS=/root/.ollama/models
ENV OLLAMA_KEEP_ALIVE=30m

# Copiamos el Modelfile
COPY config/Modelfile /config/Modelfile

EXPOSE 11434

# Script de inicio que arranca ollama en segundo plano, descarga el modelo y mantiene el servicio vivo
CMD ["sh", "-c", "ollama serve & sleep 5 && ollama pull llama3.2 && ollama create elite-architect -f /config/Modelfile && wait"]
