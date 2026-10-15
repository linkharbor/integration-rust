#!/usr/bin/env python3
"""LinkHarbor agent for Rust (Facepunch) servers.

Talks to the server's WebRCON (the websocket RCON every Rust server offers
when started with `+rcon.web 1`, default port 28016), runs `status` and
`playerlist`, and pushes the state to a LinkHarbor "Custom (API)"
integration: a live game-server card on your bio page and push
notifications for joins.

    pip install -r requirements.txt
    export LH_INGEST_URL="https://dock.at-harbor.com/v1/ingest/123"
    export LH_INGEST_SECRET="…"
    export RUST_RCON_PASSWORD="…"
    python3 agent.py --host 203.0.113.10 --port 28016 --name "My Rust Server" \
        --address "203.0.113.10:28015" --url "https://discord.gg/xyz"

Needs the `websockets` package (requirements.txt), Python 3.10+.

Events sent (each can drive a push rule in the dashboard):
    server.online    the server answered after being unreachable (or on first run)
    server.offline   the server stopped answering
    player.joined    data.player = name
    player.left      data.player = name
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid

try:
    import websockets
except ImportError:  # pragma: no cover
    print("Missing dependency: pip install -r requirements.txt", file=sys.stderr)
    sys.exit(1)

log = logging.getLogger("linkharbor")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", required=True, help="server IP / hostname")
    p.add_argument("--port", type=int, default=28016, help="RCON port (default 28016)")
    p.add_argument("--password", default=os.environ.get("RUST_RCON_PASSWORD", ""),
                   help="RCON password (default: env RUST_RCON_PASSWORD)")
    p.add_argument("--name", default="", help="card title (default: the server's hostname)")
    p.add_argument("--address", default="", help="address shown on the card, e.g. 203.0.113.10:28015")
    p.add_argument("--url", default="", help="link behind the card's button (Discord, website)")
    p.add_argument("--cta", default="Join server", help="button label")
    p.add_argument("--image-url", default="", help="banner image for the large layout")
    p.add_argument("--interval", type=int, default=60, help="seconds between polls (default 60)")
    p.add_argument("--heartbeat", type=int, default=10, help="minutes between state-only updates (0 = only on change)")
    p.add_argument("--no-player-names", action="store_true", help="don't send player names")
    p.add_argument("--ingest-url", default=os.environ.get("LH_INGEST_URL", ""))
    p.add_argument("--ingest-secret", default=os.environ.get("LH_INGEST_SECRET", ""))
    p.add_argument("--once", action="store_true", help="poll once, send, exit")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args()
    if not a.ingest_url or not a.ingest_secret:
        p.error("set LH_INGEST_URL and LH_INGEST_SECRET (or --ingest-url / --ingest-secret)")
    if not a.password:
        p.error("set RUST_RCON_PASSWORD (or --password)")
    return a


class Ingest:
    def __init__(self, url: str, secret: str) -> None:
        self.url, self.secret = url, secret

    def send(self, event: str | None, data: dict | None, state: dict) -> bool:
        body: dict = {"state": state}
        if event:
            body["event"] = event
            body["event_id"] = f"{event}-{uuid.uuid4()}"
        if data:
            body["data"] = data
        req = urllib.request.Request(
            self.url, data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {self.secret}", "Content-Type": "application/json",
                     "User-Agent": "LinkHarbor-Rust/1.0"},
        )
        for attempt in (1, 2):
            try:
                with urllib.request.urlopen(req, timeout=10) as res:
                    log.debug("← %s %s", res.status, res.read(200).decode(errors="replace"))
                    return True
            except urllib.error.HTTPError as e:
                text = e.read(300).decode(errors="replace")
                if e.code == 401:
                    log.error("401 from LinkHarbor — wrong secret or integration disconnected")
                    return False
                if e.code < 500:
                    log.error("%s from LinkHarbor: %s", e.code, text)
                    return False
                log.warning("%s from LinkHarbor (attempt %d)", e.code, attempt)
            except Exception as e:  # network
                log.warning("request failed (attempt %d): %s", attempt, e)
            time.sleep(2)
        return False


async def rcon(host: str, port: int, password: str, commands: list[str]) -> dict[str, str]:
    """Runs the commands over one WebRCON connection, returns {command: reply text}.

    Rust also broadcasts console output (Identifier 0) to every RCON client;
    those messages are skipped.
    """
    url = f"ws://{host}:{port}/{password}"  # never logged: the path is the password
    replies: dict[str, str] = {}
    async with websockets.connect(url, open_timeout=5, close_timeout=2, max_size=4_000_000) as ws:
        pending: dict[int, str] = {}
        for i, cmd in enumerate(commands, start=1):
            await ws.send(json.dumps({"Identifier": i, "Message": cmd, "Name": "WebRcon"}))
            pending[i] = cmd
        while pending:
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            ident = msg.get("Identifier") if isinstance(msg, dict) else None
            if ident in pending:
                replies[pending.pop(ident)] = str(msg.get("Message", ""))
    return replies


_PLAYERS = re.compile(r"(\d+)\s*\((\d+)\s*max\)")


def parse_status(text: str) -> dict:
    """Parses the `status` reply: `hostname: …`, `players : 3 (50 max) …`, `map     : …`, `version : …`."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip():
            fields.setdefault(key.strip().lower(), value.strip())
    m = _PLAYERS.search(fields.get("players", ""))
    return {
        "hostname": fields.get("hostname", ""),
        "players": int(m.group(1)) if m else 0,
        "max_players": int(m.group(2)) if m else 0,
        "map": fields.get("map", ""),
        "version": fields.get("version", "").split("(")[0].strip()[:40],
    }


def parse_playerlist(text: str) -> list[str]:
    """`playerlist` answers with a JSON array of {DisplayName, SteamID, Ping, …}."""
    try:
        players = json.loads(text)
    except ValueError:
        return []
    if not isinstance(players, list):
        return []
    names = [str(p.get("DisplayName", "")).strip() for p in players if isinstance(p, dict)]
    return [n for n in names if n]


async def query(a: argparse.Namespace) -> tuple[dict | None, list[str]]:
    """Returns (info, player names) or (None, []) when the server is unreachable."""
    commands = ["status"] + ([] if a.no_player_names else ["playerlist"])
    try:
        replies = await asyncio.wait_for(rcon(a.host, a.port, a.password, commands), timeout=20)
    except websockets.ConnectionClosed as e:
        log.warning("server closed the RCON connection before answering (wrong password?): %s", e)
        return None, []
    except Exception as e:
        log.debug("rcon failed: %s: %s", type(e).__name__, str(e).replace(a.password, "***"))
        return None, []
    info = parse_status(replies.get("status", ""))
    names = parse_playerlist(replies.get("playerlist", "")) if not a.no_player_names else []
    return info, names


def build_state(a: argparse.Namespace, info: dict | None, names: list[str], last_name: str) -> dict:
    s: dict = {"type": "game_server", "game": "Rust",
               "name": a.name or (info or {}).get("hostname") or last_name or "Rust server"}
    if a.address:
        s["address"] = a.address
    if a.url:
        s["url"] = a.url
    if a.cta:
        s["cta"] = a.cta
    if a.image_url:
        s["image_url"] = a.image_url
    if info is None:
        s.update(online=False, players_online=0, players=[])
        return s
    s.update(
        online=True,
        players_online=info["players"],
        players_max=info["max_players"],
        players=[] if a.no_player_names else names[:50],
        map=info["map"],
        version=info["version"],
    )
    return s


async def run(a: argparse.Namespace, ingest: Ingest) -> int:
    online: bool | None = None
    known: set[str] = set()
    last_name = ""
    last_sent = 0.0
    heartbeat_s = a.heartbeat * 60

    while True:
        info, names = await query(a)
        now_online = info is not None
        if info:
            last_name = info["hostname"] or last_name
        state = build_state(a, info, names, last_name)
        current = set(names)

        events: list[tuple[str, dict | None]] = []
        if online is None or now_online != online:
            events.append(("server.online" if now_online else "server.offline", None))
        if now_online and online is not None and not a.no_player_names:
            for n in sorted(current - known):
                events.append(("player.joined", {"player": n}))
            for n in sorted(known - current):
                events.append(("player.left", {"player": n}))

        if events:
            for ev, data in events:
                log.info("%s %s", ev, (data or {}).get("player", ""))
                await asyncio.to_thread(ingest.send, ev, data, state)
            last_sent = time.time()
        elif heartbeat_s and time.time() - last_sent >= heartbeat_s:
            log.info("heartbeat: %s players online", state.get("players_online", 0))
            await asyncio.to_thread(ingest.send, None, None, state)
            last_sent = time.time()

        online = now_online
        known = current if now_online else set()
        if a.once:
            return 0
        await asyncio.sleep(max(5, a.interval))


def main() -> int:
    a = parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("websockets").setLevel(logging.WARNING)  # its debug log prints the handshake (URL = password)
    try:
        return asyncio.run(run(a, Ingest(a.ingest_url, a.ingest_secret)))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
