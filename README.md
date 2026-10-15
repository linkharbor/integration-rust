# LinkHarbor for Rust

A single-file Python agent that asks a Rust (Facepunch) server for its
status over WebRCON and pushes the live state to your
[LinkHarbor](https://linkharbor.com) bio page: a game-server card with
Online/Offline, "12 / 100" players, player names, map and build number,
plus join/leave events for push notifications to your followers.

It runs `status` and `playerlist` through the server's RCON websocket, so it
needs the RCON password. Nothing is installed on the server. Python 3.10 or newer and
the `websockets` package.

If you do not need player names, the Steam A2S agent
([integration-steam](https://github.com/linkharbor/integration-steam)) works
for Rust without RCON.

Part of the [LinkHarbor integrations](https://github.com/linkharbor/integrations)
family, one repo per game or tool.

## Setup

```bash
git clone https://github.com/linkharbor/integration-rust
cd integration-rust
pip install -r requirements.txt

export LH_INGEST_URL="https://dock.at-harbor.com/v1/ingest/123"   # dashboard: Integrations, Custom (API)
export LH_INGEST_SECRET="…"
export RUST_RCON_PASSWORD="…"

python3 agent.py --host 203.0.113.10 --port 28016 \
  --name "My Rust Server" --address "203.0.113.10:28015" \
  --url "https://discord.gg/xyz" --interval 60
```

Start with `--once -v`. Run it as a service with `integration-rust.service`
(systemd; the three secrets go into `/etc/integration-rust.env`, `chmod 600`).
If the server runs on the same machine, `--host 127.0.0.1` works even when
the RCON port is firewalled from the outside.

## RCON port and password

Both come from the server's start line:

```
RustDedicated … +rcon.port 28016 +rcon.password "yourpassword" +rcon.web 1
```

`+rcon.web 1` is required. The RCON
password gives full console access to the server, so treat it like an admin
password: keep it in the env file, not in the command line.

## Events sent

| Event | When | Template variables |
|---|---|---|
| `server.online` | the first successful poll, or the server is back after being unreachable | `{name}`, `{players_online}`, `{players_max}`, `{map}`, `{version}` |
| `server.offline` | the server stopped answering | `{name}` |
| `player.joined` | a new name appears in `playerlist` | `{player}` and the above |
| `player.left` | a name disappears | `{player}` and the above |

The state is sent with every event and every `--heartbeat` minutes (default
10). `--no-player-names` sends counts only (and skips `playerlist`).

Suggested push rule (dashboard, Integrations, Push rules, Custom (API), event `player.joined`): batch window 10 min, title "🎮 {count} players joined", body
"{player_list} are now on {name}", minimum pause 5 min.

## Options

```
--host HOST            server IP or hostname (required)
--port PORT            RCON port (default 28016)
--password PASSWORD    RCON password (default: env RUST_RCON_PASSWORD)
--name NAME            card title (default: the server's hostname)
--address ADDRESS      address shown on the card, e.g. 203.0.113.10:28015
--url URL              link behind the card's button
--cta TEXT             button label (default "Join server")
--image-url URL        banner image (large layout)
--interval SECONDS     polling interval (default 60, minimum 5)
--heartbeat MINUTES    state-only update interval (default 10, 0 = only on change)
--no-player-names      send counts only
--once                 poll once, send, exit
-v                     verbose logging
```

## License

[MIT](LICENSE). © 2026 EMI Solutions OÜ.
