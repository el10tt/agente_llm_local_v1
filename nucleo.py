"""Lógica compartida por el agente de terminal (agente.py) y la interfaz web (webui.py).

Aquí vive todo lo que decide QUÉ se puede ejecutar y CÓMO, para que el filtro de
seguridad exista en un solo sitio: si lo cambias aquí, cambian los dos.
"""
import json
import os
import pathlib
import shlex
import subprocess
import time

# Se añade a cada mensaje tuyo cuando usas el modo chino: razona en chino, responde en español
SUFIJO_CHINO = "（请用中文思考，用西班牙语回答）"
SALIDA_MAX = 3000       # caracteres máximos de la salida de un comando que se devuelven al modelo
LOG_COMANDOS = pathlib.Path("~/.agente_comandos.log").expanduser()


def cargar_manifiesto(ruta, script):
    """Lee el manifiesto y rellena los datos de contexto."""
    texto = pathlib.Path(ruta).read_text(encoding="utf-8")
    return (texto.replace("{script}", os.path.abspath(script))
                 .replace("{directorio}", os.getcwd())
                 .replace("{usuario}", os.environ.get("USER", "desconocido")))


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
    return pathlib.Path(ruta).expanduser().read_text()[:SALIDA_MAX]


def registrar(comando, estado, origen):
    """Añade una línea al registro de comandos (~/.agente_comandos.log). Nunca rompe el agente."""
    try:
        with open(LOG_COMANDOS, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{origen}\t{estado}\t"
                    f"{json.dumps(comando, ensure_ascii=False)}\n")
    except OSError:
        pass


def ejecutar_comando(comando, *, agro, peaceful, preguntar, origen):
    """Ejecuta un comando de shell respetando los modos de permisos.

    preguntar(comando) -> bool decide si se ejecuta cuando hace falta confirmación;
    cada interfaz aporta la suya (input() en la terminal, botones en la web).
    """
    if es_interactivo(comando):
        registrar(comando, "bloqueado-interactivo", origen)
        return ("Error: ese programa es interactivo y no funciona aquí. "
                "Usa una alternativa no interactiva (por ejemplo 'top -bn1' o 'ps aux').")
    if agro:
        via = "agro"
    elif peaceful and es_solo_lectura(comando):
        via = "auto-solo-lectura"
    else:
        if not preguntar(comando):
            registrar(comando, "rechazado", origen)
            return "El usuario rechazó el comando."
        via = "aprobado"
    try:
        r = subprocess.run(comando, shell=True, capture_output=True, text=True, errors="replace",
                           timeout=60, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        registrar(comando, f"{via}+timeout", origen)
        return "Error: el comando tardó más de 60 s y se canceló."
    registrar(comando, via, origen)
    return (r.stdout + r.stderr)[:SALIDA_MAX] or "(sin salida)"


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
