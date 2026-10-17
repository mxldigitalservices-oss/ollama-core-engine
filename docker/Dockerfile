FROM ollama/ollama:latest
ENV OLLAMA_HOST=0.0.0.0:11434
ENV OLLAMA_MODELS=/root/.ollama/models
ENV OLLAMA_KEEP_ALIVE=30m
COPY config/Modelfile /config/Modelfile
COPY scripts/start.sh /start.sh
RUN chmod +x /start.sh
EXPOSE 11434
ENTRYPOINT ["/bin/sh", "/start.sh"]
git push origin HEADarranque unico con pull y create del modelo"g/Modelfile
rm 'Dockerfile'
[main 08b7937] fix: arranque unico con pull y create del modelo
 5 files changed, 15 insertions(+), 20 deletions(-)
 delete mode 100644 Dockerfile
 create mode 100644 scripts/start.sh
Enumerating objects: 16, done.
Counting objects: 100% (16/16), done.
Delta compression using up to 2 threads
Compressing objects: 100% (7/7), done.
Writing objects: 100% (9/9), 1.01 KiB | 258.00 KiB/s, done.
Total 9 (delta 3), reused 0 (delta 0), pack-reused 0
remote: Resolving deltas: 100% (3/3), completed with 3 local objects.
To https://github.com/mxldigitalservices-oss/ollama-core-engine.git
   ae32247..08b7937  HEAD -> main
josecolorvision@penguin:~/ollama-core-engine$ curl https://TU-DOMINIO.up.railway.app/api/tags
{"status":"error","code":404,"message":"Application not found","request_id":"pvKLMB8LS4CLNz0uU79b0g"}josecolorvision@penguin:~/ollama-core-engine$ curl https://TU-DOMINIO.up.railway.app/api/tags
{"status":"error","code":404,"message":"Application not found","request_id":"uQZF1ilhQVeDwIZno3UVLg"}josecolorvision@penguin:~/ollama-core-engine$ curl https://ollama-core-engine-production.up.railway.app/api/version
{"status":"error","code":502,"message":"Application failed to respond","request_id":"jFC10en2RK-1naWelt7tkg"}josecolorvision@penguin:~/ollama-core-engine$ curl https://ollama-core-engine-production.up.railway.app/api/tags
{"status":"error","code":502,"message":"Application failed to respond","request_id":"u1NHO6mQQauNcYet9o6EoQ"}josecolorvision@pcurl https://ollama-core-engine-production.up.railway.app/api/generate -d '{ailway.app/api/generate -d '{
  "model": "elite-architect",
  "prompt": "Confirma que estás activo.",
  "stream": false
}'
{"status":"error","code":502,"message":"Application failed to respond","request_id":"PpheSEvmRfeDrjYNljLL4A"}josecolorvision@penguin:~/ollama-core-engine$ cd ~/ollama-core-engine                            cd ~/ollama-core-engine

cat > scripts/start.sh << 'EOF'
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
