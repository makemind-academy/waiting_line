#!/usr/bin/env python3
"""Drive a running AppPlayer through its debug MCP to verify a sample.

Every sample in this repository is verified the same way: AppPlayer opens the
sample (a bundle folder, or a server that serves its own screen), the driver
reads what is on screen with `ui.text`, taps with `ui.tap`, types with
`ui.type`, and keeps `ui.screenshot` as the capture. Nothing renders outside
the player the reader will use.

Requirements
  * AppPlayer (macOS) with the debug MCP switched on
    (Settings → Developer → Debug MCP, or `defaults write <its bundle id>
    flutter.settings.debug_mcp -bool true` before launch).
  * APPPLAYER_APP   path to AppPlayer.app            (default: /Applications/AppPlayer.app)
  * APPPLAYER_PORT  debug MCP port                   (default: 7931; Pro listens on 7930)
  * APPPLAYER_DOMAIN preferences domain                (default: the app's bundle id)

As a library:
    from appplayer import AppPlayer
    ap = AppPlayer()
    ap.register_server("com.example.sample", "Sample", cwd="server")   # stdio: dart run bin/server.dart
    ap.restart()                       # servers are read at launch
    ap.open_server("com.example.sample")
    ap.wait_text("Shelf")
    ap.shot("captures/01_main.png")

As a command:
    python3 tools/appplayer.py register <id> <name> <server-dir>
    python3 tools/appplayer.py install  <bundle-dir>            # copies a .mbd into the player
    python3 tools/appplayer.py restart
    python3 tools/appplayer.py open-server <id> | open-bundle <id>
    python3 tools/appplayer.py text [substring] | tap <text> | tap-id <elementId> | type <text> | shot <file>
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

APP = os.environ.get("APPPLAYER_APP", "/Applications/AppPlayer.app")


def _bundle_id(app: str) -> str | None:
    """The player's own identifier: its preferences and bundle store are named after it."""
    try:
        with open(os.path.join(app, "Contents", "Info.plist"), "rb") as f:
            return plistlib.load(f).get("CFBundleIdentifier")
    except OSError:
        return None


# Which player: the one APPPLAYER_APP points at, whose preferences live under its
# own identifier (`app.appplayer` for Standard, `app.appplayer.pro` for Pro, which
# also takes `APPPLAYER_PORT=7930`). APPPLAYER_DOMAIN overrides it.
DOMAIN = os.environ.get("APPPLAYER_DOMAIN") or _bundle_id(APP) or "app.appplayer"
PORT = int(os.environ.get("APPPLAYER_PORT", "7931"))
BUNDLES = os.path.expanduser(f"~/Library/Application Support/{DOMAIN}/bundles")


# ----------------------------------------------------------------- preferences

# The player is not sandboxed, so its SharedPreferences live in the home
# Preferences folder. `defaults <domain>` would route to a stale sandbox
# container when one exists, so the file is addressed by path.
PLIST = os.path.expanduser(f"~/Library/Preferences/{DOMAIN}.plist")


def _pref_get(key: str, plist: str = PLIST):
    r = subprocess.run(["defaults", "export", plist, "-"], capture_output=True)
    if r.returncode != 0:
        return None
    return plistlib.loads(r.stdout).get(f"flutter.{key}")


def _pref_set(key: str, value: str, plist: str = PLIST) -> None:
    subprocess.run(["defaults", "write", plist, f"flutter.{key}", "-string", value], check=True)


# Fields the player rewrites on its own (connect time, the name a node reports
# about itself). A registration that differs only in these is the same one.
_VOLATILE = {"servers.v1": {"createdAt", "lastConnectedAt", "metadata"},
             "apps.v1": {"name", "metadataJson"}}


def _same(key: str, a: dict, b: dict) -> bool:
    skip = _VOLATILE.get(key, set())
    return {k: v for k, v in a.items() if k not in skip} == {k: v for k, v in b.items() if k not in skip}


def _upsert(key: str, entry: dict, plist: str = PLIST) -> bool:
    """Write [entry] unless an equal one is already there. Returns whether it wrote."""
    raw = _pref_get(key, plist)
    items = json.loads(raw) if raw else []
    current = next((i for i in items if i.get("id") == entry["id"]), None)
    if current is not None and _same(key, current, entry):
        return False
    items = [i for i in items if i.get("id") != entry["id"]]
    items.append(entry)
    _pref_set(key, json.dumps(items), plist)
    return True



def _open_file(port: int) -> str:
    """The label of the tile a sample left open — samples run in separate
    processes, so the next one learns it from here."""
    return os.path.join(tempfile.gettempdir(), f"appplayer-driver-{port}.open")


def _remember_open(port: int, label: str) -> None:
    with open(_open_file(port), "w") as f:
        f.write(label)


def _recall_open(port: int) -> str:
    try:
        return open(_open_file(port)).read().strip()
    except FileNotFoundError:
        return ""


def _tree_digest(root: str) -> str:
    h = hashlib.sha256()
    for d, dirs, files in sorted(os.walk(root)):
        dirs.sort()
        for f in sorted(files):
            p = os.path.join(d, f)
            h.update(os.path.relpath(p, root).encode())
            with open(p, "rb") as fh:
                h.update(fh.read())
    return h.hexdigest()


class AppPlayer:
    def __init__(self, port: int = PORT, app: str = APP, domain: str = DOMAIN):
        # Two players of different tiers can be driven from one script: each
        # instance addresses its own preferences file.
        self.plist = os.path.expanduser(f"~/Library/Preferences/{domain}.plist")
        self.port, self.app = port, app
        self.sid = None
        self.path = None

    # ------------------------------------------------------------- lifecycle
    def register_server(self, server_id: str, name: str, cwd: str,
                        command: str = "dart", args: list[str] | None = None,
                        description: str = "") -> None:
        """Register a stdio MCP server as a launcher app. Already registered as is:
        nothing is written and the running player is kept."""
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        wrote = _upsert("servers.v1", plist=self.plist, entry={
            "id": server_id, "name": name, "description": description,
            "transportType": "stdio",
            "transportConfig": {"command": command,
                                "arguments": args or ["run", "bin/server.dart"],
                                "workingDirectory": os.path.abspath(cwd)},
            "createdAt": now, "lastConnectedAt": None, "isFavorite": False, "metadata": None,
        })
        wrote |= _upsert("apps.v1", plist=self.plist, entry={
            "id": server_id, "name": name, "type": "server", "serverConfigId": server_id,
            "dashboardLayout": "grid", "dashboardSize": "twoByTwo", "trustLevel": "basic",
        })

    def register_http_server(self, server_id: str, name: str, base_url: str, description: str = "") -> None:
        """Register a streamable-HTTP MCP server (one process several clients share)."""
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        wrote = _upsert("servers.v1", plist=self.plist, entry={
            "id": server_id, "name": name, "description": description,
            "transportType": "streamableHttp", "transportConfig": {"baseUrl": base_url},
            "createdAt": now, "lastConnectedAt": None, "isFavorite": False, "metadata": None,
        })
        wrote |= _upsert("apps.v1", plist=self.plist, entry={
            "id": server_id, "name": name, "type": "server", "serverConfigId": server_id,
            "dashboardLayout": "grid", "dashboardSize": "twoByTwo", "trustLevel": "basic",
        })

    def remove_server(self, server_id: str) -> None:
        """Forget a server registered by `register_server` / `register_http_server`,
        so a later run's tile lookup by label meets one tile, not two. Read at
        the next launch."""
        for key in ("servers.v1", "apps.v1"):
            raw = _pref_get(key, self.plist)
            entries = json.loads(raw) if raw else []
            kept = [e for e in entries if e.get("id") != server_id and e.get("serverConfigId") != server_id]
            if len(kept) != len(entries):
                _pref_set(key, json.dumps(kept), self.plist)

    def install_bundle(self, bundle_dir: str) -> str:
        """Copy a .mbd folder into the player's bundle store. Returns its id."""
        man = json.load(open(os.path.join(bundle_dir, "manifest.json")))
        bid = man["manifest"]["id"]
        dst = os.path.join(BUNDLES, bid)
        if os.path.exists(dst) and _tree_digest(dst) == _tree_digest(bundle_dir):
            return bid
        if os.path.exists(dst):
            shutil.rmtree(dst)
        shutil.copytree(bundle_dir, dst)
        return bid

    def _listening(self) -> bool:
        r = subprocess.run(["lsof", f"-tiTCP:{self.port}", "-sTCP:LISTEN"], capture_output=True, text=True)
        return bool(r.stdout.strip())

    def restart(self, width: int = 1280, height: int = 900) -> None:
        """Bring the player to its launcher at [width] x [height], every app closed.

        A running player is reused: back to the launcher, then the tile that was
        open is disconnected from its context menu (its stdio server exits, the
        next open starts it fresh). The player is launched only when it is not
        running."""
        if self._listening() and self._back_to_launcher():
            self._disconnect_last()
            self._resize(width, height)
            return
        self._relaunch(width, height)

    def _back_to_launcher(self) -> bool:
        self.sid = self.path = None
        for _ in range(6):
            try:
                if self.at_launcher():
                    return True
                self.home()
            except Exception:  # noqa: BLE001 — a player that cannot answer gets relaunched
                return False
            time.sleep(0.8)
        return self.at_launcher()

    def _disconnect_last(self) -> None:
        """Right-click the last opened tile and choose Disconnect."""
        label = _recall_open(self.port)
        if not label:
            return
        hits = [r for t, r in self.texts(label) if t == label]
        if hits:
            r = hits[-1]
            self._text_result("ui.tap", {"x": r[0] + r[2] / 2, "y": r[1] - 20, "button": "secondary"})
            for _ in range(10):
                time.sleep(0.3)
                if self.has_text("Disconnect"):
                    self.tap("Disconnect")
                    break
        _remember_open(self.port, "")

    def _resize(self, width: int, height: int) -> None:
        # Move first: a window left against the screen's edge is clipped there
        # and comes out narrower than asked.
        subprocess.run(["osascript", "-e",
                        f'tell application "System Events" to tell process "AppPlayer" '
                        f'to set position of front window to {{40, 40}}'],
                       capture_output=True)
        subprocess.run(["osascript", "-e",
                        f'tell application "System Events" to tell process "AppPlayer" '
                        f'to set size of front window to {{{width}, {height}}}'],
                       capture_output=True)
        time.sleep(0.5)

    def _relaunch(self, width: int, height: int) -> None:
        """Quit the player that owns the debug port (only that one), launch, resize."""
        r = subprocess.run(["lsof", f"-tiTCP:{self.port}", "-sTCP:LISTEN"], capture_output=True, text=True)
        for pid in r.stdout.split():
            subprocess.run(["kill", pid])
        for _ in range(40):
            if not self._listening():
                break
            time.sleep(0.5)
        subprocess.run(["defaults", "write", self.plist, "flutter.settings.debug_mcp", "-bool", "true"], check=True)
        # -g: launch without bringing the player to the front. The gate relaunches it
        # for every sample; activating it each time takes the keyboard and the
        # screen away from whoever is working on this machine.
        subprocess.run(["open", "-g", "-n", self.app], check=True)
        for _ in range(60):
            if self._listening():
                break
            time.sleep(1)
        else:
            raise SystemExit(f"AppPlayer did not open its debug MCP on {self.port}")
        time.sleep(1.5)
        self._resize(width, height)
        self.sid = self.path = None

    # -------------------------------------------------------------------- mcp
    def _post(self, path, payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream",
                     **({"Mcp-Session-Id": self.sid} if self.sid else {})}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            if r.headers.get("Mcp-Session-Id"):
                self.sid = r.headers["Mcp-Session-Id"]
            raw = r.read().decode()
        if raw.startswith("event:") or "\ndata:" in raw or raw.startswith("data:"):
            raw = "\n".join(l[5:].strip() for l in raw.splitlines() if l.startswith("data:"))
        return json.loads(raw) if raw.strip() else None

    def _ensure(self):
        if self.path:
            return
        for p in ("/mcp", "/", "/message"):
            try:
                r = self._post(p, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                   "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                              "clientInfo": {"name": "sample-verify", "version": "1"}}})
            except Exception:
                continue
            if r and "result" in r:
                self.path = p
                self._post(p, {"jsonrpc": "2.0", "method": "notifications/initialized"})
                return
        raise SystemExit(f"no debug MCP on port {self.port} — is AppPlayer running with Debug MCP on?")

    def call(self, name, args=None):
        self._ensure()
        r = self._post(self.path, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                   "params": {"name": name, "arguments": args or {}}})
        res = r.get("result", r)
        return res

    def _text_result(self, name, args=None):
        res = self.call(name, args)
        out = [c["text"] for c in res.get("content", []) if c.get("type") == "text"]
        return json.loads(out[0]) if out and out[0].lstrip().startswith("{") else out

    # --------------------------------------------------------------- actions
    def open_bundle(self, bundle_id: str):
        return self._text_result("app.open", {"id": bundle_id})

    def at_launcher(self) -> bool:
        """The launcher is the only screen whose bar reads `AppPlayer` at the top-left."""
        return any(t == "AppPlayer" and r[1] < 30 for t, r in self.texts("AppPlayer"))

    def open_server(self, server_id: str, name: str | None = None):
        """From the launcher: tap the tile registered for this server (by its label)
        and wait until the launcher has gone — a tap that lands while the grid is
        still settling is silently lost, so it is retried."""
        label = name or self._app_name(server_id)
        for attempt in range(6):
            hits = None
            for _ in range(20):
                hits = [r for t, r in self.texts(label) if t == label]
                if hits:
                    break
                time.sleep(0.5)
            if not hits:
                raise AssertionError(f"launcher tile '{label}' not found")
            r = hits[-1]
            # The label sits under the icon; the whole tile is the tap target.
            self._text_result("ui.tap", {"x": r[0] + r[2] / 2, "y": r[1] - 20})
            for _ in range(20):
                time.sleep(0.5)
                if not self.at_launcher():
                    _remember_open(self.port, label)
                    return {"ok": True, "opened": server_id}
        raise AssertionError(f"launcher tile '{label}' did not open")

    def open_tile(self, label: str, drag_x: float = 215, drag_from_y: float = 820, drag_to_y: float = 220):
        """Open a launcher tile by label, scrolling the grid until it is on
        screen — at phone width the launcher shows a few dozen tiles at a time."""
        for _ in range(12):
            hits = [r for t, r in self.texts(label) if t == label]
            if hits:
                r = hits[-1]
                self._text_result("ui.tap", {"x": r[0] + r[2] / 2, "y": r[1] - 20})
                for _ in range(20):
                    time.sleep(0.5)
                    if not self.at_launcher():
                        _remember_open(self.port, label)
                        return {"ok": True, "opened": label}
                raise AssertionError(f"tile '{label}' did not open")
            self.drag(drag_x, drag_from_y, drag_x, drag_to_y, hold_ms=0)
            time.sleep(0.8)
        raise AssertionError(f"tile '{label}' not found after scrolling")

    def _app_name(self, server_id: str) -> str:
        raw = _pref_get("apps.v1", self.plist)
        for a in json.loads(raw) if raw else []:
            if a.get("id") == server_id:
                return a["name"]
        raise KeyError(server_id)

    def texts(self, contains: str = ""):
        r = self._text_result("ui.text", {"contains": contains} if contains else {})
        if not isinstance(r, dict):          # the page is mid-load; nothing is on screen yet
            sys.stderr.write(f"ui.text: {str(r)[:200]}\n")
            return []
        return [(t["text"], [round(x) for x in t["rect"]]) for t in r.get("texts", []) if t["rect"][0] >= 0]

    def has_text(self, contains: str) -> bool:
        return any(contains in t for t, _ in self.texts(contains))

    def wait_text(self, contains: str, timeout: float = 40) -> None:
        end = time.time() + timeout
        while time.time() < end:
            if self.has_text(contains):
                return
            time.sleep(0.5)
        raise AssertionError(f"'{contains}' never appeared on screen")

    def wait_exact(self, text: str, timeout: float = 40) -> None:
        """Wait for a widget whose whole label is `text`.

        `wait_text` matches a substring, so it also matches the button that was
        just pressed ("Dim 30 %" contains "30 %"). A readout is its own label.
        """
        end = time.time() + timeout
        while time.time() < end:
            if any(t == text for t, _ in self.texts(text)):
                return
            time.sleep(0.5)
        raise AssertionError(f"no widget reads exactly '{text}'")

    def wait_gone(self, contains: str, timeout: float = 40) -> None:
        """Wait until `contains` is off screen — a page left, a row cleared."""
        end = time.time() + timeout
        while time.time() < end:
            if not self.has_text(contains):
                return
            time.sleep(0.5)
        raise AssertionError(f"'{contains}' is still on screen")

    def expect_text(self, contains: str) -> None:
        if not self.has_text(contains):
            raise AssertionError(f"'{contains}' is not on screen")

    def expect_no_text(self, contains: str) -> None:
        if self.has_text(contains):
            raise AssertionError(f"'{contains}' is on screen and should not be")

    def expect_aligned(self, contains: str, min_rows: int = 2, edge: str = "right") -> None:
        """Values of one kind sit in one column: every text containing `contains`
        ends at the same x (right-aligned fixed cells; `edge="left"` for a
        left-aligned column). Fails on a drifting column."""
        rows = [r for t, r in self.texts(contains) if contains in t]
        if len(rows) < min_rows:
            raise AssertionError(f"'{contains}': only {len(rows)} row(s) on screen")
        if edge == "left":
            rows = [[r[0], r[1], 0, r[3]] for r in rows]
        # A header or a total may carry the same token once; a *column* is the
        # group that repeats. That group must hold `min_rows` and be the only one.
        # Layout rects are fractional, so two cells that share a column can round
        # a pixel apart. A column is a cluster, not an exact number.
        found = sorted(round(r[0] + r[2]) for r in rows)
        groups, current = [], [found[0]]
        for x in found[1:]:
            if x - current[-1] <= 1:
                current.append(x)
            else:
                groups.append(current); current = [x]
        groups.append(current)
        # Other money on the screen (a total, a tax line) forms its own small
        # group; what must not happen is the list column itself breaking up.
        if max(len(g) for g in groups) < min_rows:
            raise AssertionError(
                f"'{contains}' drifts across columns: {edge} edges {[g for g in groups]}")

    def tap(self, text: str, idx: int = 0):
        """Tap the widget showing `text`. An exact match (a button label) wins
        over a longer text that merely contains it (a row that names the same thing);
        among equals the first on screen (the control row sits above the list)."""
        hits = self.texts(text)
        exact = [r for t, r in hits if t == text]
        rows = sorted(exact or [r for t, r in hits if text in t], key=lambda r: (r[1], r[0]))
        if not rows:
            raise AssertionError(f"'{text}' is not on screen to tap")
        t = rows[idx]
        return self._text_result("ui.tap", {"x": t[0] + t[2] / 2, "y": t[1] + t[3] / 2})

    def drag(self, x: float, y: float, to_x: float, to_y: float, hold_ms: int = 600):
        """Press, hold, and move to another point — a card between columns, a node onto another."""
        return self._text_result("ui.drag", {"x": x, "y": y, "toX": to_x, "toY": to_y, "holdMs": hold_ms})

    def rect(self, text: str, idx: int = 0):
        rows = sorted([r for t, r in self.texts(text) if t == text], key=lambda r: (r[1], r[0]))
        if not rows:
            raise AssertionError(f"'{text}' is not on screen")
        return rows[idx]

    def tap_at(self, x: float, y: float):
        return self._text_result("ui.tap", {"x": x, "y": y})

    def tap_id(self, element_id: str):
        return self._text_result("ui.tap", {"elementId": element_id})

    def type(self, text: str, submit: bool = True, clear: bool = True):
        return self._text_result("ui.type", {"text": text, "clear": clear, "submit": submit})

    def shot(self, path: str, area: dict | None = None) -> None:
        res = self.call("ui.screenshot", {"area": area} if area else {})
        for c in res.get("content", []):
            if c.get("type") == "image":
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                open(path, "wb").write(base64.b64decode(c["data"]))
                return
        raise RuntimeError("no image in ui.screenshot result")

    def home(self):
        """Back to the launcher: the renderer's back control."""
        return self._text_result("ui.tap", {"elementId": "app.back"})


def _main(argv):
    ap = AppPlayer()
    cmd, rest = argv[0], argv[1:]
    if cmd == "register":
        ap.register_server(rest[0], rest[1], rest[2]); print("registered", rest[0])
    elif cmd == "install":
        print(ap.install_bundle(rest[0]))
    elif cmd == "restart":
        ap.restart(); print("restarted on", ap.port)
    elif cmd == "open-server":
        print(ap.open_server(rest[0]))
    elif cmd == "open-bundle":
        print(ap.open_bundle(rest[0]))
    elif cmd == "text":
        for t in ap.texts(rest[0] if rest else ""): print(t)
    elif cmd == "tap":
        print(ap.tap(rest[0]))
    elif cmd == "tap-id":
        print(ap.tap_id(rest[0]))
    elif cmd == "type":
        print(ap.type(rest[0]))
    elif cmd == "shot":
        ap.shot(rest[0]); print("wrote", rest[0])
    else:
        print(__doc__)


if __name__ == "__main__":
    _main(sys.argv[1:])
