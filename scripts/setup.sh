#!/bin/bash
echo "Iniciando servicio de Ollama Core Engine..."
ollama serve &
sleep 5
echo "Servicio listo."
