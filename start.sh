#!/usr/bin/env bash
# start.sh — prepara el entorno y deja Ollama + Open WebUI funcionando.
#
# Comprueba (e instala si falta, pidiendo confirmación): herramientas base,
# driver NVIDIA, Docker + compose, NVIDIA Container Toolkit, Python venv y el
# entorno virtual del agente. Después levanta los contenedores y descarga los
# modelos que falten.
#
# Uso:
#   ./start.sh              # interactivo: pregunta antes de instalar con sudo
#   ./start.sh -y           # responde "sí" a todo (para no estar delante)
#   MODELOS="qwen3:8b" ./start.sh   # elige qué modelos descargar
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

MODELOS="${MODELOS:-qwen3:8b qwen2.5-coder:7b}"
AUTO_SI=0
if [[ "${1:-}" == "-y" || "${1:-}" == "--yes" ]]; then
  AUTO_SI=1
fi

# --- Utilidades --------------------------------------------------------------
info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m ok\033[0m %s\n' "$*"; }
aviso() { printf '\033[1;33m  !\033[0m %s\n' "$*"; }
error() { printf '\033[1;31m  x\033[0m %s\n' "$*" >&2; }

trap 'error "El script falló en la línea $LINENO."' ERR

confirmar() {
  if (( AUTO_SI )); then
    return 0
  fi
  if [[ ! -t 0 ]]; then
    error "Hace falta confirmar: \"$1\" (sin terminal interactiva; usa -y)."
    return 1
  fi
  local r
  read -r -p "$1 [s/N] " r
  [[ "${r,,}" =~ ^(s|si|sí|y|yes)$ ]]
}

if ! command -v apt-get >/dev/null 2>&1; then
  error "Este script está pensado para Ubuntu/Debian (necesita apt-get)."
  exit 1
fi

SUDO=""
if [[ $EUID -ne 0 ]]; then
  if ! command -v sudo >/dev/null 2>&1; then
    error "Necesito sudo para instalar paquetes y no está disponible."
    exit 1
  fi
  SUDO="sudo"
fi

APT_ACTUALIZADO=0
apt_instalar() {
  if (( ! APT_ACTUALIZADO )); then
    $SUDO apt-get update -qq
    APT_ACTUALIZADO=1
  fi
  $SUDO apt-get install -y "$@"
}

# --- 1) Herramientas base ----------------------------------------------------
info "Comprobando herramientas base (curl, gpg)..."
BASE=()
command -v curl >/dev/null 2>&1 || BASE+=(curl)
command -v gpg  >/dev/null 2>&1 || BASE+=(gnupg)
[[ -d /etc/ssl/certs ]] || BASE+=(ca-certificates)
if (( ${#BASE[@]} )); then
  aviso "Faltan: ${BASE[*]}"
  confirmar "¿Instalarlos con apt?" || exit 1
  apt_instalar "${BASE[@]}"
fi
ok "herramientas base"

# --- 2) Driver NVIDIA --------------------------------------------------------
info "Comprobando driver NVIDIA..."
if ! command -v nvidia-smi >/dev/null 2>&1 || ! nvidia-smi >/dev/null 2>&1; then
  aviso "No detecto un driver NVIDIA funcionando (nvidia-smi falla)."
  if confirmar "¿Instalar el driver recomendado con ubuntu-drivers?"; then
    command -v ubuntu-drivers >/dev/null 2>&1 || apt_instalar ubuntu-drivers-common
    $SUDO ubuntu-drivers install
    aviso "Driver instalado. REINICIA el equipo y vuelve a ejecutar ./start.sh"
    exit 0
  fi
  error "Sin driver NVIDIA no puedo levantar Ollama con GPU (el compose la requiere)."
  exit 1
fi
ok "driver NVIDIA: $(nvidia-smi --query-gpu=name,driver_version --format=csv,noheader | head -n1)"

# --- 3) Docker + compose -----------------------------------------------------
info "Comprobando Docker..."
if ! command -v docker >/dev/null 2>&1; then
  aviso "Docker no está instalado."
  confirmar "¿Instalar Docker Engine con el script oficial (get.docker.com)?" || exit 1
  curl -fsSL https://get.docker.com | $SUDO sh
fi

if command -v systemctl >/dev/null 2>&1; then
  $SUDO systemctl enable --now docker >/dev/null 2>&1 || true
fi

GRUPO_NUEVO=0
if ! id -nG "$USER" | tr ' ' '\n' | grep -qx docker; then
  aviso "Tu usuario no está en el grupo docker: lo añado."
  $SUDO usermod -aG docker "$USER"
  GRUPO_NUEVO=1
fi

# Si aún no puedes usar docker sin sudo (grupo recién añadido), uso sudo en esta ejecución
if docker info >/dev/null 2>&1; then
  DOCKER=(docker)
else
  DOCKER=($SUDO docker)
  if (( GRUPO_NUEVO )); then
    aviso "Uso 'sudo docker' en esta ejecución. Cierra sesión y vuelve a entrar para usar docker sin sudo."
  fi
fi

if ! "${DOCKER[@]}" compose version >/dev/null 2>&1; then
  aviso "Falta el plugin docker compose."
  confirmar "¿Instalar docker-compose-plugin?" || exit 1
  apt_instalar docker-compose-plugin
fi
ok "docker $("${DOCKER[@]}" --version | cut -d' ' -f3 | tr -d ',')"

# --- 4) NVIDIA Container Toolkit (GPU dentro de Docker) ----------------------
info "Comprobando que Docker ve la GPU..."
prueba_gpu() {
  "${DOCKER[@]}" run --rm --gpus all ubuntu nvidia-smi >/dev/null 2>&1
}

if ! dpkg -s nvidia-container-toolkit >/dev/null 2>&1; then
  aviso "NVIDIA Container Toolkit no está instalado."
  confirmar "¿Instalarlo (añade el repositorio de NVIDIA a apt)?" || exit 1
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | $SUDO gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    | $SUDO tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
  APT_ACTUALIZADO=0
  apt_instalar nvidia-container-toolkit
  $SUDO nvidia-ctk runtime configure --runtime=docker
  $SUDO systemctl restart docker
fi

if ! prueba_gpu; then
  aviso "Docker no ve la GPU todavía; reconfiguro el runtime y reinicio Docker..."
  $SUDO nvidia-ctk runtime configure --runtime=docker
  $SUDO systemctl restart docker
  sleep 3
  if ! prueba_gpu; then
    error "Docker sigue sin ver la GPU. Prueba a mano: docker run --rm --gpus all ubuntu nvidia-smi"
    exit 1
  fi
fi
ok "la GPU es visible desde Docker"

# --- 5) Python + entorno virtual del agente ----------------------------------
info "Comprobando Python y el entorno virtual..."
command -v python3 >/dev/null 2>&1 || apt_instalar python3
if ! python3 -c 'import venv, ensurepip' >/dev/null 2>&1; then
  aviso "Falta python3-venv."
  confirmar "¿Instalarlo con apt?" || exit 1
  apt_instalar python3-venv
fi

if [[ ! -x .venv/bin/python ]]; then
  rm -rf .venv
  python3 -m venv .venv
  ok "creado .venv"
fi
if ! .venv/bin/python -c 'import requests' >/dev/null 2>&1; then
  .venv/bin/python -m pip install -q requests
  ok "instalado requests en .venv"
fi
ok "entorno virtual listo"

# --- 6) Comprobar archivos del proyecto --------------------------------------
for f in docker-compose.yml agente.py manifiesto_agente.md; do
  if [[ ! -f "$f" ]]; then
    error "No encuentro $f en $DIR"
    exit 1
  fi
done

# --- 7) Levantar servicios ---------------------------------------------------
info "Levantando Ollama + Open WebUI..."
"${DOCKER[@]}" compose up -d

info "Esperando a que la API de Ollama responda..."
LISTO=0
for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    LISTO=1
    break
  fi
  sleep 2
done
if (( ! LISTO )); then
  error "Ollama no respondió en 2 minutos. Mira: docker logs ollama"
  exit 1
fi
ok "Ollama responde en http://localhost:11434"

# --- 8) Modelos --------------------------------------------------------------
info "Comprobando modelos: $MODELOS"
TTY=()
[[ -t 1 ]] && TTY=(-t)
INSTALADOS="$("${DOCKER[@]}" exec ollama ollama list | awk 'NR>1 {print $1}')"
for m in $MODELOS; do
  if grep -qxF "$m" <<<"$INSTALADOS"; then
    ok "$m ya descargado"
  else
    info "Descargando $m (puede tardar y ocupar varios GB)..."
    "${DOCKER[@]}" exec "${TTY[@]}" ollama ollama pull "$m"
  fi
done

# --- 9) Cómo usarlo ----------------------------------------------------------
cat <<EOF

────────────────────────────────────────────────────────────
  Todo listo. Servicios activos:

  Chat web (Open WebUI):  http://localhost:3000
  API de Ollama:          http://localhost:11434

  Agente por terminal (desde $DIR):
    python3 agente.py                  # pregunta antes de cada comando
    python3 agente.py --peaceful       # solo lectura sin preguntar
    python3 agente.py --thinking       # ver el razonamiento
    python3 agente.py --agro           # ejecuta TODO sin preguntar (cuidado)
    Dentro: /ayuda, /reset, /modelo <nombre>, /salir

  Comprobar que el modelo corre entero en GPU (tras la 1ª pregunta):
    docker exec ollama ollama ps       # debe decir 100% GPU

  Parar todo:  ./stop.sh   (./stop.sh --help para más opciones)
────────────────────────────────────────────────────────────
EOF
if (( GRUPO_NUEVO )); then
  aviso "Cierra sesión y vuelve a entrar para usar docker sin sudo."
fi
