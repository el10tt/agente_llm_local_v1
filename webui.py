#!/usr/bin/env python3
"""Interfaz web local del agente: chat, opciones, medidor de contexto y gráficas de recursos.

Arranque:  .venv/bin/python webui.py      (o ./start.sh, que lo deja en segundo plano)
Después abre http://127.0.0.1:8765

Seguridad: esta web puede ejecutar comandos en tu máquina, así que
  - escucha SOLO en 127.0.0.1;
  - exige un token aleatorio (nuevo en cada arranque) en cada llamada a /api;
  - rechaza cualquier cabecera Host u Origin que no sea la local (anti DNS-rebinding y anti-CSRF);
  - sirve la página con una CSP estricta y sin poder ser incrustada en un iframe.
"""
import collections
import hmac
import json
import os
import pathlib
import re
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

try:
    import requests
except ImportError:
    sys.exit("Falta 'requests'. Lánzalo con el entorno del proyecto: .venv/bin/python webui.py (o ./start.sh)")

import metricas
import nucleo

DIR = pathlib.Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = int(os.environ.get("WEBUI_PORT", "8765"))
OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
URL_CHAT = f"{OLLAMA}/api/chat"
MODELO_DEFECTO = os.environ.get("AGENTE_MODELO", "qwen3:8b")
NUM_CTX_DEFECTO = int(os.environ.get("AGENTE_NUM_CTX", "8192"))
CTX_AVISO = 0.70
CTX_PELIGRO = 0.90
MANIFIESTO = DIR / "manifiesto_agente.md"
HTML = DIR / "web" / "index.html"
TOKEN = secrets.token_urlsafe(24)
HOSTS_OK = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
ORIGENES_OK = {f"http://{h}" for h in HOSTS_OK}
CHARS_TOOLS = len(json.dumps(nucleo.TOOLS, ensure_ascii=False))


class Ocupado(Exception):
    pass


class Bus:
    """Cola de eventos con identificadores: cada navegador lee desde el último id que vio."""

    def __init__(self, maximo=4000):
        self.cond = threading.Condition()
        self.eventos = collections.deque(maxlen=maximo)
        self.sig = 1

    def emitir(self, tipo, **datos):
        with self.cond:
            self.eventos.append((self.sig, json.dumps({"tipo": tipo, **datos}, ensure_ascii=False)))
            self.sig += 1
            self.cond.notify_all()

    def ultimo(self):
        with self.cond:
            return self.sig - 1

    def desde(self, id_, timeout=15):
        with self.cond:
            self.cond.wait_for(lambda: self.sig - 1 > id_, timeout)
            return [(i, t) for i, t in self.eventos if i > id_]


def uso_vacio(num_ctx):
    return {"prompt": 0, "generados": 0, "estimado": False, "tps": None, "num_ctx": num_ctx}


class Sesion:
    """Una conversación con el modelo. Solo hay una: la web es de un único usuario."""

    def __init__(self):
        self.lock = threading.RLock()
        self.bus = Bus()
        self.cfg = {"modelo": MODELO_DEFECTO, "num_ctx": NUM_CTX_DEFECTO,
                    "think": True, "peaceful": False, "agro": False, "chino": False}
        self.parar = threading.Event()
        self.ocupado = False
        self.pendiente = None      # aprobación de comando en curso
        self._resp = None          # respuesta HTTP en curso (para poder cortarla con "Parar")
        self.parcial = None        # texto de la llamada al modelo que se está generando
        self.cpt = 3.0             # caracteres por token, se recalibra con las mediciones reales
        self._reiniciar_estado()

    def _reiniciar_estado(self):
        self.messages = [{"role": "system",
                          "content": nucleo.cargar_manifiesto(MANIFIESTO, __file__)}]
        self.historial = []
        self.parcial = None
        self.uso = uso_vacio(self.cfg["num_ctx"])
        self._prompt_prev = 0
        self._nivel = 0

    # --- API hacia el navegador ---
    def estado(self):
        with self.lock:
            return {"cfg": dict(self.cfg), "historial": list(self.historial),
                    "parcial": dict(self.parcial) if self.parcial else None,
                    "uso": dict(self.uso), "ocupado": self.ocupado, "mensajes": len(self.messages),
                    "pendiente": ({"id": self.pendiente["id"], "comando": self.pendiente["comando"]}
                                  if self.pendiente else None),
                    "ultimo_evento": self.bus.ultimo()}

    def configurar(self, datos):
        with self.lock:
            for k in ("think", "peaceful", "agro", "chino"):
                if k in datos:
                    if not isinstance(datos[k], bool):
                        raise ValueError(f"'{k}' debe ser true/false")
                    self.cfg[k] = datos[k]
            if "modelo" in datos:
                m = datos["modelo"]
                if not isinstance(m, str) or not re.fullmatch(r"[\w.\-:/]{1,100}", m):
                    raise ValueError("nombre de modelo no válido")
                if m != self.cfg["modelo"]:
                    self._prompt_prev = 0
                self.cfg["modelo"] = m
            if "num_ctx" in datos:
                n = datos["num_ctx"]
                if isinstance(n, bool) or not isinstance(n, int) or not 1024 <= n <= 131072:
                    raise ValueError("num_ctx debe ser un entero entre 1024 y 131072")
                self.cfg["num_ctx"] = n
                self.uso["num_ctx"] = n
            cfg, uso = dict(self.cfg), dict(self.uso)
        self.bus.emitir("config", cfg=cfg, uso=uso)
        return cfg

    def enviar(self, texto):
        texto = (texto or "").strip()
        if not texto:
            raise ValueError("mensaje vacío")
        if len(texto) > 20000:
            raise ValueError("mensaje demasiado largo")
        with self.lock:
            if self.ocupado:
                raise Ocupado()
            self.ocupado = True
            self.parar.clear()
        threading.Thread(target=self._turno, args=(texto,), daemon=True).start()

    def reiniciar(self):
        with self.lock:
            if self.ocupado:
                raise Ocupado()
            self._reiniciar_estado()
            uso = dict(self.uso)
        self.bus.emitir("reset", uso=uso)

    def detener(self):
        self.parar.set()
        resp = self._resp
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass

    def aprobar(self, id_, ok):
        with self.lock:
            p = self.pendiente
            if not p or p["id"] != id_:
                return False
            p["ok"] = bool(ok)
            p["evento"].set()
            return True

    # --- Utilidades internas ---
    def _hist(self, entrada, tipo, **datos):
        """Guarda en el historial Y emite, bajo el mismo cerrojo: así un navegador que se
        conecta a mitad no se pierde ni duplica nada."""
        with self.lock:
            self.historial.append(entrada)
            self.bus.emitir(tipo, **datos)

    def _aviso(self, texto):
        self._hist({"rol": "aviso", "texto": texto}, "aviso", texto=texto)

    def _estimar_prompt(self):
        chars = len(json.dumps(self.messages, ensure_ascii=False)) + CHARS_TOOLS
        return chars, int(chars / self.cpt)

    # --- Un turno completo: el modelo piensa, pide herramientas, las ejecutamos... ---
    def _turno(self, texto):
        try:
            sufijo = f"\n\n{nucleo.SUFIJO_CHINO}" if self.cfg["chino"] else ""
            with self.lock:
                self.messages.append({"role": "user", "content": texto + sufijo})
            self._hist({"rol": "usuario", "texto": texto}, "usuario", texto=texto)
            while not self.parar.is_set():
                res = self._llamar_modelo()
                if res is None:
                    break
                contenido, tool_calls = res
                resp = {"role": "assistant", "content": contenido}
                if tool_calls:
                    resp["tool_calls"] = tool_calls
                with self.lock:
                    self.messages.append(resp)
                if not tool_calls:
                    break
                for i, call in enumerate(tool_calls):
                    if self.parar.is_set():  # todas las llamadas pendientes necesitan su respuesta
                        for resto in tool_calls[i:]:
                            with self.lock:
                                self.messages.append({"role": "tool", "tool_name": resto["function"]["name"],
                                                      "content": "El usuario interrumpió la ejecución."})
                        break
                    self._ejecutar_herramienta(call)
            if self.parar.is_set():
                self._aviso("Interrumpido.")
        except Exception as e:  # nunca dejar la sesión atascada en "ocupado"
            self._aviso(f"Error inesperado: {e}")
        finally:
            with self.lock:
                self.ocupado = False
                self.parcial = None
            self.bus.emitir("fin")

    def _llamar_modelo(self):
        cfg = dict(self.cfg)
        chars, est = self._estimar_prompt()
        with self.lock:
            self.parcial = {"texto": "", "razonamiento": ""}
            self.bus.emitir("inicio_llamada", prompt_est=est, num_ctx=cfg["num_ctx"])
        contenido, razon, calls, final, n = "", "", [], {}, 0
        error = None
        try:
            payload = {"model": cfg["modelo"], "messages": self.messages, "tools": nucleo.TOOLS,
                       "think": cfg["think"], "stream": True, "options": {"num_ctx": cfg["num_ctx"]}}
            with requests.post(URL_CHAT, json=payload, stream=True, timeout=(5, 300)) as r:
                self._resp = r
                if r.status_code >= 400:
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
                for linea in r.iter_lines():
                    if self.parar.is_set():
                        break
                    if not linea:
                        continue
                    datos = json.loads(linea)
                    if "error" in datos:
                        raise RuntimeError(datos["error"])
                    msg = datos.get("message", {})
                    if msg.get("thinking"):
                        razon += msg["thinking"]
                        n += 1
                        self._delta("razonamiento", msg["thinking"])
                    if msg.get("content"):
                        contenido += msg["content"]
                        n += 1
                        self._delta("texto", msg["content"])
                    if msg.get("tool_calls"):
                        calls.extend(msg["tool_calls"])
                        n += 1
                    if n and n % 8 == 0:
                        self.bus.emitir("tokens", gen=n)
                    if datos.get("done"):
                        final = datos
        except (requests.RequestException, ValueError, OSError, RuntimeError) as e:
            error = e
        finally:
            self._resp = None
        with self.lock:
            if (contenido or razon) and not self.parar.is_set():
                self.historial.append({"rol": "asistente", "texto": contenido, "razonamiento": razon})
            self.parcial = None
            self.bus.emitir("fin_llamada")
        if self.parar.is_set():
            return None
        if error is not None:
            self._aviso(f"Error hablando con Ollama: {error}")
            return None
        self._registrar_uso(final, chars, est, cfg["num_ctx"], n)
        return contenido, calls

    def _delta(self, tipo, t):
        with self.lock:
            if self.parcial is not None:
                self.parcial["texto" if tipo == "texto" else "razonamiento"] += t
            self.bus.emitir(tipo, t=t)

    def _registrar_uso(self, final, chars, est, num_ctx, n_chunks):
        medido = final.get("prompt_eval_count")
        # Con la caché de prompt, Ollama a veces no da el contador o da solo los tokens nuevos.
        # El prompt de una conversación solo crece, así que un valor menor que el anterior no es creíble.
        estimado = medido is None or medido < self._prompt_prev
        prompt = max(est, self._prompt_prev) if estimado else medido
        if not estimado and medido > 0:
            self.cpt = min(6.0, max(1.5, chars / medido))
        self._prompt_prev = max(self._prompt_prev, prompt)
        generados = final.get("eval_count", n_chunks)
        dur = final.get("eval_duration")
        tps = round(generados / (dur / 1e9), 1) if dur and generados else None
        frac = (prompt + generados) / num_ctx
        nivel = 3 if frac >= 1 else 2 if frac >= CTX_PELIGRO else 1 if frac >= CTX_AVISO else 0
        with self.lock:
            self.uso = {"prompt": prompt, "generados": generados, "estimado": estimado,
                        "tps": tps, "num_ctx": num_ctx}
            uso, mensajes = dict(self.uso), len(self.messages)
            self.bus.emitir("uso", uso=uso, mensajes=mensajes)
            subio = nivel > self._nivel
            self._nivel = nivel
        if subio and nivel == 2:
            self._aviso(f"Contexto casi lleno ({frac:.0%}). Si el modelo empieza a olvidar sus "
                        "instrucciones, usa «Nueva conversación».")
        elif subio and nivel == 3:
            self._aviso("Contexto desbordado: Ollama tiene que descartar mensajes antiguos para que "
                        "quepa el prompt. Usa «Nueva conversación».")
        if final.get("done_reason") == "length":
            self._aviso("Ollama cortó la respuesta por límite de longitud/contexto.")

    def _ejecutar_herramienta(self, call):
        nombre = call["function"]["name"]
        args = call["function"]["arguments"]
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        self._hist({"rol": "herramienta", "nombre": nombre, "args": args},
                   "herramienta", nombre=nombre, args=args)
        try:
            if nombre == "ejecutar_comando":
                res = nucleo.ejecutar_comando(
                    args["comando"], agro=self.cfg["agro"], peaceful=self.cfg["peaceful"],
                    preguntar=self._pedir_aprobacion, origen="web")
            elif nombre == "leer_archivo":
                res = nucleo.leer_archivo(**args)
            else:
                res = f"Error: herramienta desconocida '{nombre}'"
        except Exception as e:
            res = f"Error: {e}"
        res = str(res)
        with self.lock:
            self.messages.append({"role": "tool", "tool_name": nombre, "content": res})
        self._hist({"rol": "resultado", "texto": res}, "resultado", texto=res)

    def _pedir_aprobacion(self, comando):
        id_ = secrets.token_hex(4)
        p = {"id": id_, "comando": comando, "evento": threading.Event(), "ok": False}
        with self.lock:
            self.pendiente = p
            self.bus.emitir("aprobar", id=id_, comando=comando)
        while not p["evento"].wait(0.5):
            if self.parar.is_set():
                break
        ok = p["ok"] and not self.parar.is_set()
        with self.lock:
            self.pendiente = None
            self.historial.append({"rol": "aprobacion", "comando": comando, "ok": ok})
            self.bus.emitir("aprobacion_resuelta", id=id_, ok=ok)
        return ok


SESION = Sesion()
MUESTREADOR = metricas.Muestreador(OLLAMA)


def modelos_instalados():
    try:
        r = requests.get(f"{OLLAMA}/api/tags", timeout=2)
        return sorted(m["name"] for m in r.json().get("models", []))
    except (requests.RequestException, ValueError, KeyError):
        return []


class Handler(BaseHTTPRequestHandler):
    server_version = "AgenteWeb"

    def log_message(self, *args):
        pass

    # --- Respuestas ---
    def _json(self, obj, codigo=200):
        cuerpo = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(cuerpo)

    def _error(self, codigo, texto):
        self._json({"error": texto}, codigo)

    # --- Validaciones comunes ---
    def _validar(self, q, metodo_post=False):
        if self.headers.get("Host", "") not in HOSTS_OK:
            self._error(403, "Host no permitido")
            return False
        origen = self.headers.get("Origin")
        if origen is not None and origen not in ORIGENES_OK:
            self._error(403, "Origen no permitido")
            return False
        if self.path.startswith("/api/"):
            token = self.headers.get("X-Token") or (q.get("token") or [""])[0]
            if not hmac.compare_digest(token, TOKEN):
                self._error(401, "Token no válido")
                return False
        return True

    def _cuerpo_json(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        if n > 65536:
            raise ValueError("cuerpo demasiado grande")
        if n == 0:
            return {}
        datos = json.loads(self.rfile.read(n))
        if not isinstance(datos, dict):
            raise ValueError("se esperaba un objeto JSON")
        return datos

    # --- GET ---
    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if not self._validar(q):
            return
        ruta = url.path
        if ruta == "/":
            return self._pagina()
        if ruta == "/salud":
            return self._json({"ok": True})
        if ruta == "/api/estado":
            return self._json(SESION.estado())
        if ruta == "/api/modelos":
            return self._json({"modelos": modelos_instalados()})
        if ruta == "/api/metricas":
            try:
                desde = float((q.get("desde") or ["0"])[0])
            except ValueError:
                desde = 0.0
            return self._json(MUESTREADOR.instantanea(desde))
        if ruta == "/api/eventos":
            try:
                desde = int((q.get("desde") or ["0"])[0])
            except ValueError:
                desde = 0
            return self._sse(desde)
        self._error(404, "No encontrado")

    def _pagina(self):
        try:
            html = HTML.read_text(encoding="utf-8")
        except OSError:
            return self._error(500, f"Falta {HTML}")
        nonce = secrets.token_urlsafe(16)
        cuerpo = html.replace("__TOKEN__", TOKEN).replace("__NONCE__", nonce).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                         "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
                         "form-action 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(cuerpo)

    def _sse(self, desde):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        ultimo = desde
        try:
            while True:
                items = SESION.bus.desde(ultimo, timeout=15)
                if not items:
                    self.wfile.write(b": ping\n\n")
                else:
                    ultimo = items[-1][0]
                    self.wfile.write("".join(f"id: {i}\ndata: {t}\n\n" for i, t in items).encode())
                self.wfile.flush()
        except OSError:
            pass  # el navegador cerró la pestaña

    # --- POST ---
    def do_POST(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if not self._validar(q, metodo_post=True):
            return
        try:
            datos = self._cuerpo_json()
            ruta = url.path
            if ruta == "/api/enviar":
                SESION.enviar(datos.get("texto"))
                return self._json({"ok": True}, 202)
            if ruta == "/api/aprobar":
                return self._json({"ok": SESION.aprobar(datos.get("id"), datos.get("ok"))})
            if ruta == "/api/config":
                return self._json({"cfg": SESION.configurar(datos)})
            if ruta == "/api/reset":
                SESION.reiniciar()
                return self._json({"ok": True})
            if ruta == "/api/parar":
                SESION.detener()
                return self._json({"ok": True})
            self._error(404, "No encontrado")
        except Ocupado:
            self._error(409, "El agente está ocupado: espera o pulsa Parar")
        except (ValueError, KeyError) as e:
            self._error(400, str(e))

    def do_OPTIONS(self):  # sin CORS: ningún otro origen puede hacer peticiones con cabeceras propias
        self._error(405, "Método no permitido")


class Servidor(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    if not MANIFIESTO.exists():
        sys.exit(f"No encuentro el manifiesto: {MANIFIESTO}")
    MUESTREADOR.start()
    try:
        servidor = Servidor((HOST, PORT), Handler)
    except OSError as e:
        sys.exit(f"No puedo abrir {HOST}:{PORT} ({e}). ¿Ya está en marcha? Cambia el puerto con WEBUI_PORT=...")
    print(f"Interfaz web del agente en http://{HOST}:{PORT}  (modelo {SESION.cfg['modelo']}, Ollama en {OLLAMA})",
          flush=True)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nAdiós.")
    finally:
        SESION.detener()


if __name__ == "__main__":
    main()
