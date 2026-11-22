import argparse
import atexit
import json
import os
import pathlib
import re
import readline  # activa Ctrl+L, flechas, historial, Ctrl+R...
import shlex
import shutil
import subprocess
import sys

REQUISITOS = ["requests"]
VENV_DIR = pathlib.Path(__file__).resolve().parent / ".venv"


# --- Entorno virtual: se comprueba ANTES de importar nada externo ---
def en_venv():
    return sys.prefix != sys.base_prefix


def preguntar(texto):
    """Pregunta s/N; sin terminal interactiva devuelve False."""
    if not sys.stdin.isatty():
        return False
    try:
        return input(f"{texto} [s/N] ").strip().lower() in ("s", "si", "sí", "y", "yes")
    except EOFError:
        return False


def preparar_entorno():
    """Si no estamos en un venv, usa (o crea) el .venv de la carpeta del script y se relanza dentro."""
    if en_venv():
        return  # ya estamos en un venv: seguimos en silencio

    python_venv = VENV_DIR / "bin" / "python"
    if not python_venv.exists():
        print(f"No estás en un entorno virtual y no existe uno en {VENV_DIR}.")
        if not preguntar("¿Crearlo ahora e instalar las dependencias?"):
            print("Sin entorno virtual no continúo. Puedes crearlo a mano con: python3 -m venv .venv")
            sys.exit(1)
        existia = VENV_DIR.exists()
        try:
            subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)], check=True)
            subprocess.run([str(python_venv), "-m", "pip", "install", "-q", *REQUISITOS], check=True)
        except (subprocess.CalledProcessError, OSError) as e:
            if not existia:
                shutil.rmtree(VENV_DIR, ignore_errors=True)
            print(f"No pude crear el entorno ({e}).\n"
                  "En Ubuntu suele faltar el paquete: sudo apt install python3-venv")
            sys.exit(1)
        print("Entorno creado.")

    # Relanza este mismo script con el Python del venv (el shell de fuera no se toca)
    if os.environ.get("AGENTE_REEXEC"):
        print(f"No consigo entrar en el entorno virtual. Revisa {VENV_DIR}")
        sys.exit(1)
    os.environ["AGENTE_REEXEC"] = "1"
    os.environ["VIRTUAL_ENV"] = str(VENV_DIR)
    os.execv(str(python_venv), [str(python_venv), *sys.argv])


def asegurar_dependencias():
    """Ya dentro de un venv: comprueba que estén los paquetes necesarios."""
    faltan = []
    for paquete in REQUISITOS:
        try:
            __import__(paquete)
        except ImportError:
            faltan.append(paquete)
    if faltan:
        print(f"Faltan paquetes en este entorno: {', '.join(faltan)}")
        if not preguntar("¿Instalarlos ahora con pip?"):
            sys.exit(1)
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", *faltan], check=True)


preparar_entorno()
asegurar_dependencias()

import requests  # noqa: E402  (va aquí a propósito, después de preparar el entorno)

URL = "http://localhost:11434/api/chat"
MODEL = "qwen3:8b"
THINK = True                # True = el modelo razona antes de responder (más lento, más listo)
NUM_CTX = 8192
PEACEFUL = False            # lo activa el flag --peaceful
AGRO = False                # lo activa el flag --agro
MOSTRAR_PENSAMIENTO = False # lo activa el flag --thinking
RAZONAR_CHINO = False       # lo activa el flag --chinese
# Se añade a cada mensaje tuyo cuando usas --chinese: razona en chino, responde en español
SUFIJO_CHINO = "（请用中文思考，用西班牙语回答）"
HISTORIAL = pathlib.Path("~/.agente_history").expanduser()
MANIFIESTO = pathlib.Path(__file__).resolve().parent / "manifiesto_agente.md"
AYUDA = """Comandos:
  /reset          borra la conversación y recarga el manifiesto
  /modelo [x]     muestra o cambia el modelo (ej. /modelo qwen2.5-coder:7b)
  /ayuda          muestra esto
  /salir          sale (también Ctrl+D)
Atajos: Ctrl+L limpia la pantalla, flechas = historial, Ctrl+C interrumpe.
Flags al arrancar: --peaceful (solo lectura sin preguntar), --thinking (ver el razonamiento),
                   --chinese (el modelo razona en chino y responde en español),
                   --agro (ejecuta TODOS los comandos sin preguntar)."""


def cargar_manifiesto():
    """Lee el manifiesto y rellena los datos de contexto."""
    texto = MANIFIESTO.read_text(encoding="utf-8")
    return (texto.replace("{script}", os.path.abspath(__file__))
                 .replace("{directorio}", os.getcwd())
                 .replace("{usuario}", os.environ.get("USER", "desconocido")))


# --- Historial de lo que escribes, persistente entre sesiones ---
try:
    readline.read_history_file(HISTORIAL)
except FileNotFoundError:
    pass
readline.set_history_length(1000)
atexit.register(readline.write_history_file, HISTORIAL)


# --- Modo --peaceful: qué se considera "solo lectura" ---
SOLO_LECTURA = {
    "ls", "cat", "head", "tail", "pwd", "whoami", "id", "uname", "uptime", "df", "du",
    "free", "ps", "stat", "grep", "egrep", "fgrep", "wc", "cut", "tr", "echo", "printf",
    "which", "realpath", "dirname", "basename", "lsblk", "lscpu", "nproc",
    "find", "sort", "uniq", "date", "hostname", "nvidia-smi",
    "lspci", "lsusb", "lsmod", "vmstat", "sensors", "top",
}
SIN_ARGUMENTOS = {"hostname", "nvidia-smi"}  # solo valen sin argumentos
FIND_PROHIBIDO = ("-exec", "-ok", "-delete", "-fprint", "-fls")


def segmento_ok(seg):
    """Un comando simple (sin pipes): ¿es de solo lectura?"""
    if not seg or seg[0] not in SOLO_LECTURA:
        return False
    cmd, args = seg[0], seg[1:]
    if cmd in SIN_ARGUMENTOS:
        return not args
    if cmd == "find":  # find puede borrar, ejecutar o escribir con estas opciones
        return not any(a.startswith(FIND_PROHIBIDO) for a in args)
    if cmd == "sort":  # sort -o / --output escribe en un archivo
        return not any(a.startswith("--") or (a.startswith("-") and "o" in a) for a in args)
    if cmd == "uniq":  # uniq entrada salida escribe en "salida"
        return sum(1 for a in args if not a.startswith("-")) <= 1
    if cmd == "date":  # date <fecha> o -s cambian la hora del sistema
        return all(a.startswith("+") or a in ("-u", "--utc") for a in args)
    if cmd == "top":   # solo "top -bn1" y similares; el top interactivo se cuelga
        cortas = [a for a in args if a.startswith("-") and not a.startswith("--")]
        return any("b" in a for a in cortas) and any("n" in a for a in cortas)
    return True


def es_solo_lectura(comando):
    """True solo si el comando es claramente de solo lectura. Ante la duda: False (se pregunta)."""
    if any(c in comando for c in ("\n", "\r", "`", "$(", "<(", ">(")):
        return False
    try:
        lex = shlex.shlex(comando, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return False
    segmentos, actual = [], []
    for t in tokens:
        if t == "|":
            segmentos.append(actual)
            actual = []
        elif t and set(t) <= set(";&<>()"):  # ;  &&  ||  >  >>  <  etc.
            return False
        else:
            actual.append(t)
    segmentos.append(actual)
    return all(segmento_ok(s) for s in segmentos)


# --- Protección contra programas interactivos ---
INTERACTIVOS = {"htop", "btop", "vim", "vi", "nano", "less", "more", "man", "watch", "tmux", "screen"}


def es_interactivo(comando):
    try:
        tokens = shlex.split(comando)
    except ValueError:
        return False
    if not tokens:
        return False
    if tokens[0] in INTERACTIVOS:
        return True
    # "top" solo es válido en modo batch (-b)
    return tokens[0] == "top" and not any(a.startswith("-") and "b" in a for a in tokens[1:])


# --- Las "manos": funciones reales que ejecuta tu máquina ---
def leer_archivo(ruta):
    return pathlib.Path(ruta).expanduser().read_text()[:3000]


def ejecutar_comando(comando):
    if es_interactivo(comando):
        return ("Error: ese programa es interactivo y no funciona aquí. "
                "Usa una alternativa no interactiva (por ejemplo 'top -bn1' o 'ps aux').")
    if not (AGRO or (PEACEFUL and es_solo_lectura(comando))):
        if input(f"\n¿Ejecutar '{comando}'? [s/N] ").lower() != "s":
            return "El usuario rechazó el comando."
    r = subprocess.run(comando, shell=True, capture_output=True, text=True,
                       timeout=60, stdin=subprocess.DEVNULL)
    return (r.stdout + r.stderr)[:3000] or "(sin salida)"


FUNCIONES = {"leer_archivo": leer_archivo, "ejecutar_comando": ejecutar_comando}

TOOLS = [
    {"type": "function", "function": {
        "name": "leer_archivo",
        "description": "Lee el contenido de un archivo de texto",
        "parameters": {"type": "object",
                       "properties": {"ruta": {"type": "string", "description": "Ruta del archivo"}},
                       "required": ["ruta"]}}},
    {"type": "function", "function": {
        "name": "ejecutar_comando",
        "description": "Ejecuta un comando de shell en Linux y devuelve su salida. "
                       "Solo comandos NO interactivos que terminen solos (nada de htop, vim, nano, less; "
                       "usa 'top -bn1' en vez de 'top').",
        "parameters": {"type": "object",
                       "properties": {"comando": {"type": "string", "description": "Comando a ejecutar"}},
                       "required": ["comando"]}}},
]

# --- Colores ---
BASE = "\033[0m\033[38;5;16m\033[48;5;231m"   # texto negro sobre fondo blanco
NEGRITA = "\033[1m\033[48;5;252m"             # negrita negra sobre gris claro
FIN_NEGRITA = "\033[22m\033[48;5;231m"        # vuelve a texto normal sobre blanco
GRIS = "\033[2m\033[38;5;245m"                # razonamiento en gris tenue
RESET = "\033[0m"


def renderizar(texto):
    """Pinta la respuesta: fondo blanco, letras negras y **negrita** sobre gris claro."""
    texto = re.sub(r"\*\*(.+?)\*\*", lambda m: NEGRITA + m.group(1) + FIN_NEGRITA, texto)
    # \033[K rellena hasta el final de la línea con el fondo, para que quede un bloque uniforme
    return "\n".join(BASE + linea + "\033[K\033[0m" for linea in texto.split("\n"))


def chat(model, messages):
    """Bucle del agente con streaming: pensar -> usar herramienta -> pensar... hasta dar respuesta final."""
    while True:
        contenido, tool_calls = "", []
        pensando = False
        try:
            with requests.post(URL, json={
                "model": model, "messages": messages, "tools": TOOLS,
                "think": THINK, "stream": True, "options": {"num_ctx": NUM_CTX},
            }, stream=True, timeout=300) as r:
                r.raise_for_status()
                for linea in r.iter_lines():
                    if not linea:
                        continue
                    msg = json.loads(linea).get("message", {})
                    if msg.get("thinking") and MOSTRAR_PENSAMIENTO:
                        if not pensando:
                            print(f"{GRIS}💭 Razonando...{RESET}")
                            pensando = True
                        print(f"{GRIS}{msg['thinking']}{RESET}", end="", flush=True)
                    if msg.get("content"):
                        contenido += msg["content"]
                    if msg.get("tool_calls"):
                        tool_calls.extend(msg["tool_calls"])
        except requests.RequestException as e:
            print(f"\nError hablando con Ollama: {e}")
            return
        if pensando:
            print()  # salto de línea tras el razonamiento

        resp = {"role": "assistant", "content": contenido}
        if tool_calls:
            resp["tool_calls"] = tool_calls
        messages.append(resp)

        if not tool_calls:
            print(renderizar(contenido))
            return

        for call in tool_calls:
            nombre = call["function"]["name"]
            args = call["function"]["arguments"]
            print(f"  [herramienta] {nombre}({args})")
            try:
                resultado = FUNCIONES[nombre](**args)
            except Exception as e:
                resultado = f"Error: {e}"
            messages.append({"role": "tool", "tool_name": nombre, "content": str(resultado)})


def main():
    global PEACEFUL, AGRO, MOSTRAR_PENSAMIENTO, RAZONAR_CHINO
    parser = argparse.ArgumentParser(description="Agente local con Ollama")
    parser.add_argument("--peaceful", action="store_true",
                        help="ejecuta sin preguntar los comandos de solo lectura; "
                             "el resto sigue pidiendo confirmación")
    parser.add_argument("--agro", action="store_true",
                        help="ejecuta TODOS los comandos sin pedir confirmación")
    parser.add_argument("--thinking", action="store_true",
                        help="muestra el razonamiento del modelo en tiempo real")
    parser.add_argument("--chinese", action="store_true",
                        help="pide al modelo que razone en chino y responda en español")
    args = parser.parse_args()
    PEACEFUL = args.peaceful
    AGRO = args.agro
    MOSTRAR_PENSAMIENTO = args.thinking
    RAZONAR_CHINO = args.chinese

    model = MODEL
    try:
        messages = [{"role": "system", "content": cargar_manifiesto()}]
    except FileNotFoundError:
        print(f"No encuentro el manifiesto: {MANIFIESTO}")
        return
    if AGRO:
        modo = "AGRO (ejecuta todo sin preguntar)"
    elif PEACEFUL:
        modo = "peaceful (solo lectura sin preguntar)"
    else:
        modo = "normal (pregunta siempre)"
    ver = "mostrando razonamiento" if MOSTRAR_PENSAMIENTO else "razonamiento oculto"
    idioma = ", razonando en chino" if RAZONAR_CHINO else ""
    print(f"Agente listo con {model}. Modo {modo}, {ver}{idioma}. Escribe /ayuda para ver los comandos.")

    while True:
        try:
            entrada = input("\n> ").strip()
        except EOFError:          # Ctrl+D
            print()
            break
        except KeyboardInterrupt:  # Ctrl+C en el prompt
            print("\n(Ctrl+D o /salir para salir)")
            continue

        if not entrada:
            continue
        if entrada in ("/salir", "/exit"):
            break
        if entrada == "/ayuda":
            print(AYUDA)
            continue
        if entrada == "/reset":
            try:
                messages = [{"role": "system", "content": cargar_manifiesto()}]
            except FileNotFoundError:
                print(f"No encuentro el manifiesto: {MANIFIESTO}")
                continue
            print("Conversación reiniciada (manifiesto recargado).")
            continue
        if entrada.startswith("/modelo"):
            partes = entrada.split(maxsplit=1)
            if len(partes) == 2:
                model = partes[1]
            print(f"Modelo actual: {model}")
            continue

        contenido = f"{entrada}\n\n{SUFIJO_CHINO}" if RAZONAR_CHINO else entrada
        messages.append({"role": "user", "content": contenido})
        try:
            chat(model, messages)
        except KeyboardInterrupt:  # Ctrl+C mientras el modelo responde
            print("\n(interrumpido)")


if __name__ == "__main__":
    main()
