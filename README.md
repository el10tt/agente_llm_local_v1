# LLM local con agente (Ollama + Open WebUI + agente en Python)

Proyecto casero para correr un modelo de lenguaje **en local**, con GPU, y tener encima un **agente** que pueda ejecutar comandos en la máquina. Montado en octubre de 2026 en un Ubuntu con una RTX 3070 (8 GB de VRAM), 32 GB de RAM y un i7.

La idea: el modelo es el **cerebro** (solo genera texto) y el agente son las **manos** (un script que ejecuta lo que el modelo pide y le devuelve el resultado).

## Qué hay aquí

| Archivo | Para qué sirve |
|---|---|
| `docker-compose.yml` | Levanta Ollama (motor del modelo, con GPU) y Open WebUI (chat en el navegador) |
| `agente.py` | El agente: chat por terminal con el modelo, con herramientas para leer archivos y ejecutar comandos |
| `manifiesto_agente.md` | El "system prompt" del agente (quién es, qué herramientas tiene, cómo debe comportarse). Se edita sin tocar el código |

Se crean al usar el agente (no se versionan):

- `.venv/`: entorno virtual de Python, lo crea el propio script si no existe (te pregunta antes).
- `~/.agente_history`: historial de lo que escribes en el prompt del agente.

## Piezas y cómo encajan

```
Tú ──► agente.py ──┐
                   ├──► Ollama (puerto 11434) ──► modelo en la GPU
Tú ──► Open WebUI ─┘          (contenedor)
       (puerto 3000)
```

- **Modelo** (`qwen3:8b`, `qwen2.5-coder:7b`): un archivo con los pesos de la red. Por sí solo no hace nada.
- **Ollama**: carga el modelo en la GPU y lo expone como API HTTP.
- **Open WebUI**: interfaz web tipo ChatGPT que habla con Ollama. Es opcional.
- **agente.py**: bucle "el modelo piensa → pide una herramienta → el script la ejecuta → el modelo lee el resultado → ...", hasta dar una respuesta final.

## Requisitos (en el host, Ubuntu)

1. **GPU NVIDIA con el driver propietario.** Comprueba con `nvidia-smi`. Si no sale tu GPU: `sudo ubuntu-drivers install` y reinicia.
2. **Docker Engine con el plugin compose.**
   ```bash
   curl -fsSL https://get.docker.com | sh
   sudo usermod -aG docker $USER    # cierra sesión y vuelve a entrar para usar docker sin sudo
   ```
3. **NVIDIA Container Toolkit**, para que Docker vea la GPU:
   ```bash
   curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
   curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
     sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
     sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
   sudo apt update && sudo apt install -y nvidia-container-toolkit
   sudo nvidia-ctk runtime configure --runtime=docker
   sudo systemctl restart docker
   ```
4. **Comprobar que la GPU llega a Docker:**
   ```bash
   docker run --rm --gpus all ubuntu nvidia-smi
   ```
5. **Python 3 con `venv`** para el agente: `sudo apt install python3 python3-venv`. El único paquete de Python que necesita es `requests`, y el script lo instala solo en su `.venv`.
6. Opcional: `jq` (`sudo apt install jq`) para filtrar respuestas de la API con `curl`.

## Puesta en marcha

```bash
# 1) Levantar Ollama + Open WebUI
docker compose up -d

# 2) Descargar los modelos (se guardan en un volumen de Docker)
docker exec -it ollama ollama pull qwen3:8b
docker exec -it ollama ollama pull qwen2.5-coder:7b

# 3) Comprobar que el modelo corre entero en la GPU (debe poner 100% GPU)
docker exec ollama ollama ps
```

- Chat en el navegador: http://localhost:3000 (la primera vez te pide crear un usuario local).
- API de Ollama: http://localhost:11434 (los puertos solo escuchan en `127.0.0.1`).

## Usar el agente

```bash
python3 agente.py                          # normal: pregunta antes de cada comando
python3 agente.py --peaceful               # ejecuta sin preguntar solo comandos de solo lectura
python3 agente.py --agro                   # ejecuta TODO sin preguntar (cuidado)
python3 agente.py --thinking               # muestra el razonamiento del modelo en directo
python3 agente.py --chinese                # pide razonar en chino y responder en español
python3 agente.py --peaceful --thinking    # los flags se pueden combinar
```

Si no estás en un entorno virtual, el script usa el `.venv` de su carpeta (o te pregunta si quieres crearlo) y se relanza dentro. No modifica el shell desde el que lo lanzas.

### Comandos dentro del agente

| Comando | Qué hace |
|---|---|
| `/reset` | Borra la conversación y recarga el manifiesto |
| `/modelo [nombre]` | Muestra o cambia el modelo (ej. `/modelo qwen2.5-coder:7b`) |
| `/ayuda` | Lista de comandos |
| `/salir` o `Ctrl+D` | Salir |

Atajos: `Ctrl+L` limpia la pantalla, flechas para el historial, `Ctrl+R` busca en él, `Ctrl+C` interrumpe una respuesta.

### Modos de permisos

- **Normal:** cada comando pide confirmación `[s/N]`.
- **`--peaceful`:** lista blanca de comandos de solo lectura (`ls`, `cat`, `grep`, `find`, `ps`, `df`, `nvidia-smi`, `top -bn1`...). Con pipes entre ellos vale; con redirecciones (`>`), encadenados (`;`, `&&`) o sustituciones (`$(...)`) siempre pregunta. Cualquier programa fuera de la lista también pregunta.
- **`--agro`:** ejecuta todo sin preguntar. Manda sobre `--peaceful`.
- En todos los modos se bloquean los programas interactivos (`htop`, `vim`, `nano`, `less`...), porque sin terminal se quedarían colgados.

## Notas y limitaciones

- **Hardware:** con 8 GB de VRAM caben modelos de 7–9B en cuantización Q4. Si el modelo no cabe entero en la GPU, Ollama pasa capas a la RAM y todo va mucho más lento. Se comprueba con `ollama ps`.
- **Contexto:** el agente usa `NUM_CTX = 8192`. Con el razonamiento activado y salidas largas de comandos se llena rápido; `/reset` lo vacía. Subirlo gasta más VRAM.
- **Memoria de la conversación:** la lista de mensajes vive en la RAM del script. Al salir se pierde. El modelo no recuerda nada entre peticiones.
- **Seguridad:** `--agro` ejecuta lo que el modelo decida, y un 8B se equivoca. `sudo` falla porque los comandos corren sin teclado. El filtro de `--peaceful` es una lista blanca prudente, no una garantía absoluta. No hay registro de los comandos ejecutados (no quedan en el historial de bash).
- **Ollama no tiene autenticación.** Mantén los puertos ligados a `127.0.0.1`. Si los abres a la red, cualquiera podrá usar la GPU.
- **Idioma del razonamiento:** Qwen3 razona en inglés por defecto y `--chinese` solo funciona a ratos. No afecta a la calidad de la respuesta final.

## Mantenimiento

```bash
docker compose stop        # para los contenedores (los conserva)
docker compose start       # los arranca de nuevo
docker compose down        # los elimina (modelos y chats siguen en los volúmenes)
docker compose down -v     # elimina también los volúmenes: borra modelos y chats
docker compose pull && docker compose up -d   # actualizar imágenes
docker ps                  # ver qué está corriendo
docker logs ollama         # logs de Ollama
```

Los modelos viven en el volumen `ollama`; los usuarios y chats de Open WebUI, en el volumen `open-webui`.

## Uso rápido de la API (sin interfaz)

```bash
curl -s http://localhost:11434/api/generate -d '{
  "model": "qwen3:8b",
  "prompt": "Hola, ¿qué tal?",
  "think": false,
  "stream": false
}' | jq -r .response
```

Ollama también expone un endpoint compatible con OpenAI en `http://localhost:11434/v1`, útil para herramientas que lo soportan (por ejemplo, el autocompletado del editor con `qwen2.5-coder:7b`).

## Ideas para más adelante

- Una herramienta `info_sistema` que devuelva RAM, CPU y GPU ya resumidos (a un modelo pequeño le rinde dar las cosas masticadas).
- Registrar en un archivo los comandos que ejecuta el agente.
- Probar modelos más grandes repartidos entre GPU y RAM (`qwen3:14b`, `qwen3:30b-a3b`), con la contrapartida de la velocidad.
- Generación de imágenes en local con ComfyUI + Flux (en 8 GB, versión cuantizada GGUF). Hay que parar Ollama antes (`docker exec ollama ollama stop qwen3:8b`) para liberar VRAM.
