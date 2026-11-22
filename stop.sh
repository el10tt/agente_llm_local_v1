#!/usr/bin/env bash
# stop.sh — para Ollama + Open WebUI y libera la GPU.
#
# Uso:
#   ./stop.sh           # para los contenedores (se conservan; ./start.sh los reanuda)
#   ./stop.sh --down    # para y ELIMINA los contenedores (modelos y chats siguen en los volúmenes)
#   ./stop.sh --purge   # como --down y además BORRA los volúmenes: modelos y chats (pide confirmación)
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m ok\033[0m %s\n' "$*"; }
aviso() { printf '\033[1;33m  !\033[0m %s\n' "$*"; }
error() { printf '\033[1;31m  x\033[0m %s\n' "$*" >&2; }

MODO="stop"
case "${1:-}" in
  ""|--stop) MODO="stop" ;;
  --down)    MODO="down" ;;
  --purge)   MODO="purge" ;;
  -h|--help)
    sed -n '2,7p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
  *)
    error "Opción desconocida: $1 (usa --help)"
    exit 1
    ;;
esac

if ! command -v docker >/dev/null 2>&1; then
  ok "Docker no está instalado: no hay nada que parar."
  exit 0
fi

SUDO=""
[[ $EUID -ne 0 ]] && SUDO="sudo"
if docker info >/dev/null 2>&1; then
  DOCKER=(docker)
else
  DOCKER=($SUDO docker)
fi

if [[ ! -f docker-compose.yml ]]; then
  error "No encuentro docker-compose.yml en $DIR"
  exit 1
fi

# Aviso si hay un agente abierto en otra terminal (perderá la conexión con Ollama)
if pgrep -f 'python.*agente\.py' >/dev/null 2>&1; then
  aviso "Hay un agente.py en ejecución en otra terminal; dejará de poder hablar con Ollama."
fi

case "$MODO" in
  stop)
    info "Parando contenedores..."
    "${DOCKER[@]}" compose stop
    ;;
  down)
    info "Parando y eliminando contenedores (los volúmenes se conservan)..."
    "${DOCKER[@]}" compose down
    ;;
  purge)
    aviso "Esto borrará los volúmenes: se pierden los modelos descargados y los chats de Open WebUI."
    if [[ ! -t 0 ]]; then
      error "Sin terminal interactiva no borro volúmenes."
      exit 1
    fi
    read -r -p "Escribe BORRAR para continuar: " r
    if [[ "$r" != "BORRAR" ]]; then
      info "Cancelado."
      exit 0
    fi
    "${DOCKER[@]}" compose down -v
    ;;
esac

ok "Servicios parados."
if command -v nvidia-smi >/dev/null 2>&1; then
  USADA="$(nvidia-smi --query-gpu=memory.used --format=csv,noheader 2>/dev/null | head -n1 || true)"
  [[ -n "$USADA" ]] && info "VRAM en uso ahora: $USADA"
fi
info "Para volver a arrancar: ./start.sh"
