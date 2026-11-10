# =============================================================================
#  monitor_examen.py
#  Descarga los archivos que llegan al chat "Examen" de Telegram
#  en la carpeta indicada como argumento.
#
#  DEPENDENCIAS:
#      pip install telethon
#
#  REQUISITOS:
#      nitragram_cred.secret  ─┐
#      nitragram.session      ─┘ en la misma carpeta que este script
#
#  USO:
#      pythonw.exe monitor_examen.py
# =============================================================================

import os
import sys   # solo para ocultar la consola en Windows
import json
import asyncio

# =============================================================================
#   CONFIGURACIÓN  ← editar aquí
# =============================================================================

CHAT_OBJETIVO    = "Examen"
CARPETA_DESTINO  = r"C:\Users\TuUsuario\Descargas\Examen"

_DIR    = os.path.dirname(os.path.abspath(__file__))
CRED    = os.path.join(_DIR, "nitragram_cred.secret")
SESSION = os.path.join(_DIR, "nitragram")

# =============================================================================
#   OCULTAR CONSOLA
# =============================================================================

if sys.platform == "win32":
    import ctypes
    _hwnd = ctypes.windll.kernel32.GetConsoleWindow()
    if _hwnd:
        ctypes.windll.user32.ShowWindow(_hwnd, 0)

# =============================================================================
#   MONITOREO
# =============================================================================

from telethon import TelegramClient, events


async def monitorear(carpeta_destino: str) -> None:
    os.makedirs(carpeta_destino, exist_ok=True)

    with open(CRED, encoding="utf-8") as f:
        cred = json.load(f)

    async with TelegramClient(SESSION, int(cred["api_id"]), cred["api_hash"]) as client:

        @client.on(events.NewMessage(chats=CHAT_OBJETIVO))
        async def _on_mensaje(event):
            if event.message.media:
                await event.message.download_media(file=carpeta_destino)

        await client.run_until_disconnected()


# =============================================================================
#   PUNTO DE ENTRADA
# =============================================================================

if __name__ == "__main__":
    asyncio.run(monitorear(CARPETA_DESTINO))
