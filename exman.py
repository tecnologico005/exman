# =============================================================================
#  monitor_examen.py
#  Descarga los archivos que llegan al chat "Examen" de Telegram en la carpeta
#  indicada y responde "Recibido" en el mismo chat.
#
#  Es resistente a conexiones inestables:
#    - Reconexión automática infinita (Telethon) + bucle externo con espera
#      progresiva si algo falla del todo.
#    - Descargas con reintentos y archivos temporales (nunca quedan archivos
#      a medias en la carpeta destino).
#    - Recuperación de mensajes perdidos: se guarda el último mensaje
#      procesado y se revisa el chat al conectar y cada SONDEO_SEGUNDOS,
#      así no se pierde ningún archivo aunque se cayera internet.
#    - Registro de eventos en monitor_examen.log (no hay consola).
#
#  DEPENDENCIAS:
#      pip install telethon
#
#  REQUISITOS:
#      nitragram_cred.secret  ─┐
#      nitragram.session      ─┤ en la misma carpeta que este script
#      carpeta.txt            ─┘ con la carpeta destino entre comillas, en una
#                                sola línea. Ejemplo:  "C:\Users\Juan\Descargas\Examen"
#
#  USO:
#      pythonw.exe monitor_examen.py
# =============================================================================

import os
import sys   # solo para ocultar la consola en Windows
import json
import asyncio
import logging
from logging.handlers import RotatingFileHandler

# =============================================================================
#   CONFIGURACIÓN  ← editar aquí
# =============================================================================

CHAT_OBJETIVO    = "Examen"
MENSAJE_CONFIRMA = "Recibido"

SONDEO_SEGUNDOS     = 60    # cada cuánto revisar el chat por si se perdió algo
INTENTOS_DESCARGA   = 5     # reintentos por cada descarga
INTENTOS_ENVIO      = 5     # reintentos al enviar el "Recibido"
MAX_CICLOS_FALLIDOS = 5     # tras tantos ciclos fallando, se omite ese mensaje
ESPERA_MIN          = 5     # espera inicial (s) al reiniciar tras un error
ESPERA_MAX          = 300   # espera máxima (s)

_DIR    = os.path.dirname(os.path.abspath(__file__))
CRED    = os.path.join(_DIR, "nitragram_cred.secret")
SESSION = os.path.join(_DIR, "nitragram")
CARPETA_TXT = os.path.join(_DIR, "carpeta.txt")
ESTADO  = os.path.join(_DIR, "monitor_examen_estado.json")
LOG     = os.path.join(_DIR, "monitor_examen.log")

# =============================================================================
#   OCULTAR CONSOLA
# =============================================================================

if sys.platform == "win32":
    import ctypes
    _hwnd = ctypes.windll.kernel32.GetConsoleWindow()
    if _hwnd:
        ctypes.windll.user32.ShowWindow(_hwnd, 0)

# =============================================================================
#   LOG
# =============================================================================

log = logging.getLogger("monitor_examen")
log.setLevel(logging.INFO)
_h = RotatingFileHandler(LOG, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
_h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(_h)

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError

# =============================================================================
#   ESTADO PERSISTENTE (último mensaje procesado)
# =============================================================================

def cargar_estado() -> dict:
    try:
        with open(ESTADO, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"ultimo_id": None}


def guardar_estado(estado: dict) -> None:
    tmp = ESTADO + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(estado, f)
    os.replace(tmp, ESTADO)

# =============================================================================
#   CARPETA DESTINO (desde carpeta.txt)
# =============================================================================

def leer_carpeta() -> str:
    """Lee la ruta de carpeta.txt: una sola línea, entre comillas."""
    with open(CARPETA_TXT, encoding="utf-8-sig") as f:
        texto = f.read().strip()
    ruta = texto.strip('"\'“”‘’').strip()
    if not ruta:
        raise ValueError("carpeta.txt está vacío")
    return os.path.expandvars(os.path.expanduser(ruta))


# =============================================================================
#   UTILIDADES
# =============================================================================

async def con_reintentos(fabrica, intentos: int, descripcion: str):
    """Ejecuta `await fabrica()` con reintentos y espera progresiva."""
    for n in range(1, intentos + 1):
        try:
            return await fabrica()
        except FloodWaitError as e:
            log.warning("%s: FloodWait, esperando %s s", descripcion, e.seconds)
            await asyncio.sleep(e.seconds + 1)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            espera = min(2 ** n, 60)
            log.warning("%s: intento %d/%d falló (%s). Reintento en %d s",
                        descripcion, n, intentos, e, espera)
            await asyncio.sleep(espera)
    raise RuntimeError(f"{descripcion}: agotados {intentos} intentos")


def ruta_libre(carpeta: str, nombre: str) -> str:
    base, ext = os.path.splitext(nombre)
    ruta = os.path.join(carpeta, nombre)
    i = 1
    while os.path.exists(ruta):
        ruta = os.path.join(carpeta, f"{base} ({i}){ext}")
        i += 1
    return ruta


def limpiar_carpeta(carpeta: str) -> None:
    for nombre in os.listdir(carpeta):
        try:
            os.remove(os.path.join(carpeta, nombre))
        except OSError:
            pass


async def descargar(mensaje, carpeta_destino: str) -> str:
    """Descarga a una carpeta temporal y mueve al destino solo si termina bien."""
    tmp_dir = os.path.join(carpeta_destino, ".descargando")
    os.makedirs(tmp_dir, exist_ok=True)

    async def _intento():
        limpiar_carpeta(tmp_dir)  # descarta restos de intentos anteriores
        ruta_tmp = await mensaje.download_media(file=tmp_dir)
        if not ruta_tmp or not os.path.exists(ruta_tmp):
            raise IOError("La descarga no produjo ningún archivo")
        return ruta_tmp

    try:
        ruta_tmp = await con_reintentos(
            _intento, INTENTOS_DESCARGA, f"Descarga msg {mensaje.id}")
        destino = ruta_libre(carpeta_destino, os.path.basename(ruta_tmp))
        os.replace(ruta_tmp, destino)
        return destino
    finally:
        limpiar_carpeta(tmp_dir)

# =============================================================================
#   SINCRONIZACIÓN (única vía de procesado: eventos y sondeo la usan)
# =============================================================================

async def sincronizar(client, entidad, estado, lock, carpeta_destino, fallos):
    async with lock:
        if estado["ultimo_id"] is None:
            # Primera ejecución: no descargar el historial, solo marcar el punto de partida
            ultimos = await client.get_messages(entidad, limit=1)
            estado["ultimo_id"] = ultimos[0].id if ultimos else 0
            guardar_estado(estado)
            log.info("Punto de partida fijado en mensaje %s", estado["ultimo_id"])
            return

        nuevos = [m async for m in client.iter_messages(
            entidad, min_id=estado["ultimo_id"], reverse=True)]

        for m in nuevos:
            if m.media:
                try:
                    destino = await descargar(m, carpeta_destino)
                    log.info("Descargado msg %s → %s", m.id, destino)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    fallos[m.id] = fallos.get(m.id, 0) + 1
                    log.error("Msg %s: descarga fallida (ciclo %d/%d): %s",
                              m.id, fallos[m.id], MAX_CICLOS_FALLIDOS, e)
                    if fallos[m.id] < MAX_CICLOS_FALLIDOS:
                        return  # se reintenta en el próximo ciclo, en orden
                    log.error("Msg %s: se omite tras demasiados fallos", m.id)
                    estado["ultimo_id"] = m.id
                    guardar_estado(estado)
                    continue

                try:
                    await con_reintentos(
                        lambda: client.send_message(entidad, MENSAJE_CONFIRMA),
                        INTENTOS_ENVIO, f"Confirmación msg {m.id}")
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    # El archivo ya está guardado; no se vuelve a descargar
                    log.error("Msg %s: no se pudo enviar la confirmación: %s", m.id, e)

            estado["ultimo_id"] = m.id
            guardar_estado(estado)


async def sondeo(client, entidad, estado, lock, carpeta, fallos):
    """Red de seguridad: revisa el chat periódicamente."""
    while True:
        await asyncio.sleep(SONDEO_SEGUNDOS)
        try:
            await sincronizar(client, entidad, estado, lock, carpeta, fallos)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("Sondeo falló (se reintentará): %s", e)

# =============================================================================
#   MONITOREO
# =============================================================================

async def buscar_chat(client):
    """Localiza el chat por nombre entre los diálogos."""
    async def _buscar():
        async for d in client.iter_dialogs():
            if d.name == CHAT_OBJETIVO:
                return d.entity
        raise LookupError(f'No se encontró el chat "{CHAT_OBJETIVO}"')
    return await con_reintentos(_buscar, 10, "Buscar chat")


async def monitorear(carpeta_destino: str) -> None:
    os.makedirs(carpeta_destino, exist_ok=True)

    with open(CRED, encoding="utf-8") as f:
        cred = json.load(f)

    estado = cargar_estado()
    fallos: dict = {}
    espera = ESPERA_MIN

    while True:  # bucle externo: ante cualquier error, se reconstruye todo
        client = None
        tarea = None
        try:
            client = TelegramClient(
                SESSION, int(cred["api_id"]), cred["api_hash"],
                connection_retries=-1,    # reintentos de conexión infinitos
                retry_delay=2,
                request_retries=5,
                auto_reconnect=True,
                timeout=20,
                catch_up=True,            # recupera updates perdidos al reconectar
            )
            await client.connect()
            if not await client.is_user_authorized():
                log.critical("La sesión no está autorizada. Genera nitragram.session de nuevo.")
                return

            entidad = await buscar_chat(client)
            lock = asyncio.Lock()
            log.info("Conectado. Vigilando el chat '%s'", CHAT_OBJETIVO)

            async def _on_mensaje(event):
                try:
                    await sincronizar(client, entidad, estado, lock,
                                      carpeta_destino, fallos)
                except Exception as e:
                    log.warning("Error procesando evento (el sondeo lo reintentará): %s", e)

            client.add_event_handler(_on_mensaje, events.NewMessage(chats=entidad))

            await sincronizar(client, entidad, estado, lock, carpeta_destino, fallos)
            tarea = asyncio.create_task(
                sondeo(client, entidad, estado, lock, carpeta_destino, fallos))

            espera = ESPERA_MIN  # conexión lograda: reinicia la espera
            await client.run_until_disconnected()
            log.warning("Desconectado. Reconectando...")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("Error general: %s. Reiniciando en %d s", e, espera)
            await asyncio.sleep(espera)
            espera = min(espera * 2, ESPERA_MAX)
        finally:
            if tarea:
                tarea.cancel()
            if client:
                try:
                    await client.disconnect()
                except Exception:
                    pass


# =============================================================================
#   PUNTO DE ENTRADA
# =============================================================================

if __name__ == "__main__":
    try:
        _carpeta = leer_carpeta()
        log.info("Carpeta destino: %s", _carpeta)
        asyncio.run(monitorear(_carpeta))
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("El script terminó por un error no controlado")
