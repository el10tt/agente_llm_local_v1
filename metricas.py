"""Muestreo periódico de recursos: sistema, Ollama y el propio agente (solo biblioteca estándar).

Mide CPU y RAM leyendo /proc, la GPU con nvidia-smi y el reparto GPU/CPU del modelo
con /api/ps de Ollama. Los procesos de Ollama se identifican por el nombre "ollama":
aunque corran dentro de Docker, el host los ve en /proc.
"""
import collections
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.request


def _leer_cpu():
    with open("/proc/stat") as f:
        v = [int(x) for x in f.readline().split()[1:9]]
    return sum(v), v[3] + v[4]  # total de ticks, ticks inactivos (idle + iowait)


def _leer_ram():
    datos = {}
    with open("/proc/meminfo") as f:
        for linea in f:
            clave, _, resto = linea.partition(":")
            if clave in ("MemTotal", "MemAvailable"):
                datos[clave] = int(resto.split()[0]) * 1024
    return datos["MemTotal"] - datos["MemAvailable"], datos["MemTotal"]


def _leer_proc(pid):
    """(ticks de CPU, RSS en bytes) de un proceso; None si no se puede leer."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            s = f.read()
        campos = s[s.rindex(")") + 2:].split()   # tras el nombre: estado, ppid, ...
        ticks = int(campos[11]) + int(campos[12])  # utime + stime
        rss = 0
        with open(f"/proc/{pid}/status") as f:
            for linea in f:
                if linea.startswith("VmRSS:"):
                    rss = int(linea.split()[1]) * 1024
                    break
        return ticks, rss
    except (OSError, ValueError, IndexError):
        return None


def _pids_ollama():
    pids = []
    for d in os.listdir("/proc"):
        if d.isdigit():
            try:
                with open(f"/proc/{d}/comm") as f:
                    if f.read().strip() == "ollama":
                        pids.append(d)
            except OSError:
                pass
    return pids


def _numero(texto):
    try:
        return float(texto)
    except ValueError:
        return None  # "[N/A]" y similares


def _leer_gpu():
    if not shutil.which("nvidia-smi"):
        return None
    try:
        salida = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4).stdout.strip().splitlines()[0]
        util, usada, total, temp, pot = [x.strip() for x in salida.split(",")]
        return {"util": _numero(util), "vram_usada": _numero(usada), "vram_total": _numero(total),
                "temp": _numero(temp), "potencia": _numero(pot)}
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return None


class Muestreador(threading.Thread):
    def __init__(self, ollama_url, intervalo=2.0, historial=180):
        super().__init__(daemon=True)
        self.ollama = ollama_url.rstrip("/")
        self.intervalo = intervalo
        self.muestras = collections.deque(maxlen=historial)
        self.ps = None
        self.ollama_ok = False
        self.lock = threading.Lock()

    def _consultar_ps(self):
        try:
            with urllib.request.urlopen(f"{self.ollama}/api/ps", timeout=1.5) as r:
                datos = json.load(r)
        except (OSError, ValueError):
            return False, None
        modelos = datos.get("models") or []
        if not modelos:
            return True, None
        m = modelos[0]
        return True, {"modelo": m.get("name"), "tam": m.get("size"), "tam_vram": m.get("size_vram"),
                      "ctx": m.get("context_length")}

    def _muestra(self, prev):
        total, idle = _leer_cpu()
        pids = _pids_ollama()
        leidos = [p for p in (_leer_proc(pid) for pid in pids) if p]
        tk_o, rss_o = sum(p[0] for p in leidos), sum(p[1] for p in leidos)
        propio = _leer_proc("self") or (0, 0)
        usada, ram_total = _leer_ram()
        cpu = cpu_o = cpu_a = None
        if prev and total > prev["total"]:
            d = total - prev["total"]
            cpu = 100.0 * (1 - (idle - prev["idle"]) / d)
            cpu_o = max(0.0, 100.0 * (tk_o - prev["tk_o"]) / d)
            cpu_a = max(0.0, 100.0 * (propio[0] - prev["tk_a"]) / d)
        estado = {"total": total, "idle": idle, "tk_o": tk_o, "tk_a": propio[0]}
        ok, ps = self._consultar_ps()
        with self.lock:
            self.ollama_ok, self.ps = ok, ps
        muestra = {"t": round(time.time(), 2), "cpu": cpu, "cpu_ollama": cpu_o, "cpu_agente": cpu_a,
                   "ram_usada": usada, "ram_total": ram_total, "ram_ollama": rss_o, "ram_agente": propio[1],
                   "gpu": _leer_gpu()}
        return muestra, estado

    def run(self):
        prev = None
        while True:
            inicio = time.time()
            try:
                muestra, prev = self._muestra(prev)
                with self.lock:
                    self.muestras.append(muestra)
            except Exception:  # un fallo puntual de lectura no debe matar el muestreo
                pass
            time.sleep(max(0.2, self.intervalo - (time.time() - inicio)))

    def instantanea(self, desde=0.0):
        with self.lock:
            return {"muestras": [m for m in self.muestras if m["t"] > desde],
                    "ps": self.ps, "ollama_ok": self.ollama_ok, "intervalo": self.intervalo}
