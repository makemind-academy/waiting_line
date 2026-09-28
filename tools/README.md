# tools

What every `verify.py` in this tree imports. Nothing here is a sample.

- `appplayer.py` — drives one AppPlayer over its debug MCP (port 7931): registers a server app or installs a bundle,
  restarts the player, opens the app from the launcher, reads the painted text, taps, types, screenshots. The same
  player a reader has; the driver only presses what a finger would.
- `mcpclient.py` — a minimal MCP client over stdio or streamable HTTP, for the claims a sample makes about its server
  alone (what it refuses, what two tools return in sequence). `serve_http` starts a server in `--http=<port>` mode and
  stops its whole process group.
- `serve_bundle.dart` — copied into a server's `bin/` when the server serves its own pages: registers `ui://app`,
  `ui://app/info` and `ui://pages/<name>` from a `.mbd` folder next to it.

## Requirements

- AppPlayer Standard (macOS) with the debug MCP enabled. `APPPLAYER_APP` may point at a specific `.app`; the default is
  the installed one. `APPPLAYER_PORT` defaults to 7931.
- One player. The driver kills whatever owns the port and relaunches that app; it never starts a second instance.
- Dart SDK on `PATH` for the servers; a C compiler for the simulators (`cc`).
