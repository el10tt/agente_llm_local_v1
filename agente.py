import argparse
import atexit
import json
import os
import pathlib
import re
import readline  # activa Ctrl+L, flechas, historial, Ctrl+R...
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
import nucleo  # noqa: E402
from nucleo import SUFIJO_CHINO, TOOLS, leer_archivo  # noqa: E402

URL = "http://localhost:11434/api/chat"
MODEL = "qwen3:8b"
THINK = True                # True = el modelo razona antes de responder (más lento, más listo)
NUM_CTX = 8192
CTX_AVISO = 0.70            # el medidor de contexto se pone amarillo a partir de este %
CTX_PELIGRO = 0.90          # ...y rojo a partir de este (conviene hacer /reset)
PEACEFUL = False            # lo activa el flag --peaceful
AGRO = False                # lo activa el flag --agro
MOSTRAR_PENSAMIENTO = False # lo activa el flag --thinking
RAZONAR_CHINO = False       # lo activa el flag --chinese
HISTORIAL = pathlib.Path("~/.agente_history").expanduser()
MANIFIESTO = pathlib.Path(__file__).resolve().parent / "manifiesto_agente.md"
AYUDA = """Comandos:
  /reset          borra la conversación y recarga el manifiesto
  /modelo [x]     muestra o cambia el modelo (ej. /modelo qwen2.5-coder:7b)
  /ctx            muestra cuánto contexto se está usando (también se ve tras cada respuesta)
  /ayuda          muestra esto
  /salir          sale (también Ctrl+D)
Atajos: Ctrl+L limpia la pantalla, flechas = historial, Ctrl+C interrumpe.
Flags al arrancar: --peaceful (solo lectura sin preguntar), --thinking (ver el razonamiento),
                   --chinese (el modelo razona en chino y responde en español),
                   --agro (ejecuta TODOS los comandos sin preguntar)."""


def cargar_manifiesto():
    """Lee el manifiesto y rellena los datos de contexto."""
    return nucleo.cargar_manifiesto(MANIFIESTO, __file__)


# --- Historial de lo que escribes, persistente entre sesiones ---
try:
    readline.read_history_file(HISTORIAL)
except FileNotFoundError:
    pass
readline.set_history_length(1000)
atexit.register(readline.write_history_file, HISTORIAL)


# --- Las "manos": el filtro de seguridad y las herramientas viven en nucleo.py ---
def ejecutar_comando(comando):
    return nucleo.ejecutar_comando(
        comando, agro=AGRO, peaceful=PEACEFUL, origen="cli",
        preguntar=lambda c: input(f"\n¿Ejecutar '{c}'? [s/N] ").lower() == "s")


FUNCIONES = {"leer_archivo": leer_archivo, "ejecutar_comando": ejecutar_comando}

# --- Colores ---
BASE = "\033[0m\033[38;5;16m\033[48;5;231m"   # texto negro sobre fondo blanco
NEGRITA = "\033[1m\033[48;5;252m"             # negrita negra sobre gris claro
FIN_NEGRITA = "\033[22m\033[48;5;231m"        # vuelve a texto normal sobre blanco
GRIS = "\033[2m\033[38;5;245m"                # razonamiento en gris tenue
RESET = "\033[0m"
VERDE = "\033[32m"
AMARILLO = "\033[33m"
ROJO = "\033[31m"


def renderizar(texto):
    """Pinta la respuesta: fondo blanco, letras negras y **negrita** sobre gris claro."""
    texto = re.sub(r"\*\*(.+?)\*\*", lambda m: NEGRITA + m.group(1) + FIN_NEGRITA, texto)
    # \033[K rellena hasta el final de la línea con el fondo, para que quede un bloque uniforme
    return "\n".join(BASE + linea + "\033[K\033[0m" for linea in texto.split("\n"))


# --- Medidor de contexto ---
# Última medición, para poder consultarla con /ctx
ULTIMO_USO = {"prompt": 0, "generados": 0, "estimado": False}


def formatear_uso():
    """Barra de uso del contexto a partir de la última medición."""
    prompt, generados = ULTIMO_USO["prompt"], ULTIMO_USO["generados"]
    usados = prompt + generados
    frac = usados / NUM_CTX
    color = VERDE if frac < CTX_AVISO else AMARILLO if frac < CTX_PELIGRO else ROJO
    llenos = min(10, round(frac * 10))
    barra = "█" * llenos + "░" * (10 - llenos)
    aprox = "~" if ULTIMO_USO["estimado"] else ""
    linea = (f"{color}[ctx {aprox}{usados}/{NUM_CTX} {barra} {frac:.0%} "
             f"| prompt {aprox}{prompt} + generado {generados}]{RESET}")
    if frac >= 1:
        linea += (f"\n{ROJO}⚠ Contexto desbordado: Ollama tiene que descartar mensajes antiguos "
                  f"para que quepa el prompt. Haz /reset.{RESET}")
    elif frac >= CTX_PELIGRO:
        linea += (f"\n{ROJO}⚠ Contexto casi lleno: el modelo puede empezar a olvidar sus "
                  f"instrucciones. Considera /reset.{RESET}")
    return linea


def medidor_contexto(datos):
    """Lee los contadores del último trozo del stream de Ollama y devuelve la barra de uso.

    prompt_eval_count = tokens que se le han enviado al modelo (manifiesto + historial + resultados).
    eval_count        = tokens que ha generado, razonamiento incluido: ocupan ventana mientras
                        se generan, aunque luego no se reenvíen en el historial.
    Si Ollama no da prompt_eval_count (pasa con la caché de prompt en algunas versiones),
    se reutiliza el último valor conocido y se marca con ~.
    """
    estimado = "prompt_eval_count" not in datos
    prompt = ULTIMO_USO["prompt"] if estimado else datos["prompt_eval_count"]
    ULTIMO_USO.update(prompt=prompt, generados=datos.get("eval_count", 0), estimado=estimado)
    salida = formatear_uso()
    if datos.get("done_reason") == "length":
        salida += f"\n{ROJO}⚠ Ollama cortó la respuesta por límite de longitud/contexto.{RESET}"
    return salida


def chat(model, messages):
    """Bucle del agente con streaming: pensar -> usar herramienta -> pensar... hasta dar respuesta final."""
    while True:
        contenido, tool_calls = "", []
        pensando = False
        final = {}  # último trozo del stream (done=true): trae los contadores de tokens
        try:
            with requests.post(URL, json={
                "model": model, "messages": messages, "tools": TOOLS,
                "think": THINK, "stream": True, "options": {"num_ctx": NUM_CTX},
            }, stream=True, timeout=300) as r:
                r.raise_for_status()
                for linea in r.iter_lines():
                    if not linea:
                        continue
                    datos = json.loads(linea)
                    msg = datos.get("message", {})
                    if datos.get("done"):
                        final = datos
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

        uso = medidor_contexto(final) if final else ""

        if not tool_calls:
            print(renderizar(contenido))
            if uso:
                print(uso)
            return

        if uso:
            print(uso)
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
            ULTIMO_USO.update(prompt=0, generados=0, estimado=False)
            print("Conversación reiniciada (manifiesto recargado).")
            continue
        if entrada == "/ctx":
            if ULTIMO_USO["prompt"] or ULTIMO_USO["generados"]:
                print(formatear_uso())
            else:
                print("Aún no hay mediciones: haz primero una petición.")
            print(f"Mensajes en la conversación: {len(messages)}")
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
