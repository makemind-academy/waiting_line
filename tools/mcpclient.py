#!/usr/bin/env python3
"""A minimal MCP client over stdio, for the claims a sample makes about its server.

The screen is verified in AppPlayer (appplayer.py). Some claims are about the
server alone — what it refuses, what it never says, what two tools return in
sequence — and those are cheaper to state against the server directly than to
read off a screen. This client speaks just enough of the protocol for that:
initialize, tools/list, tools/call, resources/list, resources/read.

    from mcpclient import Server
    with Server(["dart", "run", "bin/server.dart"], cwd="booking_server") as s:
        print(s.tools())
        print(s.call("book.unsafe", {"who": "Smith", "slot": "10:30"}))
"""
from __future__ import annotations

import json
import subprocess
import threading


class Server:
    def __init__(self, command: list[str], cwd: str | None = None, env: dict | None = None):
        self.proc = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self._id = 0
        self._lock = threading.Lock()
        self._request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "sample-verify", "version": "1"}})
        self._notify("notifications/initialized")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()

    # ----------------------------------------------------------------- wire
    def _send(self, msg: dict) -> None:
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def _notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _request(self, method: str, params: dict | None = None):
        with self._lock:
            self._id += 1
            rid = self._id
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
            while True:
                line = self.proc.stdout.readline()
                if not line:
                    raise RuntimeError(f"server closed while waiting for {method}")
                line = line.strip()
                if not line.startswith("{"):
                    continue
                msg = json.loads(line)
                if msg.get("id") == rid:
                    if "error" in msg:
                        raise RuntimeError(msg["error"])
                    return msg["result"]

    # ------------------------------------------------------------------ api
    def tools(self) -> list[dict]:
        return self._request("tools/list")["tools"]

    def tool_names(self) -> list[str]:
        return [t["name"] for t in self.tools()]

    def call(self, name: str, args: dict | None = None):
        """Returns the parsed JSON of the first text content, or the raw text."""
        res = self._request("tools/call", {"name": name, "arguments": args or {}})
        texts = [c["text"] for c in res.get("content", []) if c.get("type") == "text"]
        if res.get("isError"):
            raise RuntimeError(texts[0] if texts else res)
        if not texts:
            return res
        try:
            return json.loads(texts[0])
        except ValueError:
            return texts[0]

    def call_text(self, name: str, args: dict | None = None) -> str:
        res = self._request("tools/call", {"name": name, "arguments": args or {}})
        return "\n".join(c["text"] for c in res.get("content", []) if c.get("type") == "text")

    def resources(self) -> list[str]:
        return [r["uri"] for r in self._request("resources/list")["resources"]]

    def read(self, uri: str):
        res = self._request("resources/read", {"uri": uri})
        text = res["contents"][0].get("text", "")
        try:
            return json.loads(text)
        except ValueError:
            return text


class HttpServer(Server):
    """The same client over streamable HTTP, for a server that several clients
    share — the player on one side, this script on the other."""

    def __init__(self, url: str):
        import urllib.request
        self._url = url
        self._sid = None
        self._urllib = urllib.request
        self._id = 0
        self._lock = threading.Lock()
        self._request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "sample-verify", "version": "1"}})
        self._notify("notifications/initialized")

    def close(self):
        pass

    def _post(self, msg: dict):
        req = self._urllib.Request(self._url, data=json.dumps(msg).encode(),
                                   headers={"Content-Type": "application/json",
                                            "Accept": "application/json, text/event-stream",
                                            **({"Mcp-Session-Id": self._sid} if self._sid else {})},
                                   method="POST")
        with self._urllib.urlopen(req, timeout=30) as r:
            if r.headers.get("Mcp-Session-Id"):
                self._sid = r.headers["Mcp-Session-Id"]
            raw = r.read().decode()
        if raw.startswith("event:") or "\ndata:" in raw or raw.startswith("data:"):
            raw = "\n".join(l[5:].strip() for l in raw.splitlines() if l.startswith("data:"))
        return json.loads(raw) if raw.strip() else None

    def _notify(self, method: str, params: dict | None = None) -> None:
        self._post({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _request(self, method: str, params: dict | None = None):
        with self._lock:
            self._id += 1
            msg = self._post({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}})
            if msg is None:
                return None
            if "error" in msg:
                raise RuntimeError(msg["error"])
            return msg["result"]


class serve_http:
    """Start a sample server in `--http=<port>` mode and stop the whole process
    group on exit. `dart run` forks a VM; terminating only the launcher would
    leave the port held by an orphan, and the next run would talk to it."""

    def __init__(self, command: list[str], cwd: str, port: int, wait: float = 20):
        import os
        self.command, self.cwd, self.port, self.wait = command, cwd, port, wait
        self._os = os

    def __enter__(self):
        import socket
        import time
        self.proc = subprocess.Popen(self.command, cwd=self.cwd, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, text=True, start_new_session=True)
        end = time.time() + self.wait
        while time.time() < end:
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", self.port)) == 0:
                    return self
            if self.proc.poll() is not None:
                raise RuntimeError(f"server exited: {self.proc.stderr.read()[-400:]}")
            time.sleep(0.3)
        self.__exit__()
        raise RuntimeError(f"server did not open port {self.port}")

    def __exit__(self, *_):
        import signal
        try:
            self._os.killpg(self._os.getpgid(self.proc.pid), signal.SIGTERM)
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self._os.killpg(self._os.getpgid(self.proc.pid), signal.SIGKILL)
            except Exception:
                pass
