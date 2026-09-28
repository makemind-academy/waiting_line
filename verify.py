#!/usr/bin/env python3
"""waiting-line: one server, two screens; the counter seats a party and the door screen follows without anyone touching it."""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools"))
from appplayer import AppPlayer  # noqa: E402
from mcpclient import HttpServer, serve_http  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "waiting_server")
CAP = os.path.join(HERE, "captures")

PORT, URL = 8768, "http://localhost:8768/mcp"
with serve_http(["dart", "run", "bin/server.dart", f"--http={PORT}"], cwd=SERVER, port=PORT):
    staff = HttpServer(URL)
    src = open(os.path.join(SERVER, "bin", "server.dart")).read()
    assert "estimate" in src and "'estimate': '" in src, "the estimate is computed on the server"

    ap = AppPlayer()
    ap.register_http_server("com.makemind.sample.waiting", "Harbor Table", URL)
    ap.restart()
    ap.open_server("com.makemind.sample.waiting")
    ap.wait_text("Front door")
    # The host stand writes three parties in; the door screen shows them
    # through its subscription, untouched.
    for name, size in (("Walker", 2), ("Hughes", 4), ("Bennett", 3), ("Fletcher", 2)):
        staff.call("line.add", {"name": name, "size": size})
    ap.wait_text("Fletcher")
    ap.expect_aligned("ahead", min_rows=2)

    def door():
        parties = [t for t, r in ap.texts("parties")][0]
        people = [t for t, _ in ap.texts(" people") if t.endswith(" people") and not t[0] == "#"][0]
        est = [t for t, _ in ap.texts(" min") if t.endswith(" min")][0]
        n = [t for t, r in ap.texts() if t.isdigit() and r[3] > 50][0]
        return f"{n} parties, {people}, about {est}"

    door_before = door()
    print(f"   door reads: {door_before}")
    ap.shot(f"{CAP}/01_door_before.png")

    # A counter — the staff tablet — calls the party at the front while the door
    # is on screen. Nobody touches the door; it follows through its subscription.
    called = staff.call("line.call")["notice"]
    print(f"   counter called: {called}")
    for _ in range(20):
        if not ap.has_text("Walker"):
            break
        time.sleep(0.5)
    else:
        raise AssertionError("the door still shows Walker after the counter called them")
    door_after = door()
    print(f"   door now reads: {door_after} — nobody edited this screen")
    print(f"   estimate {door_before.split('about ')[1]} -> {door_after.split('about ')[1]} because the list is 4 -> 3")
    ap.shot(f"{CAP}/04_door_after_call.png")

    # The counter, opened fresh — a second device joining the same queue. It
    # shows the three that are left, each with its wait, and a button that
    # carries the party at the front.
    ap.restart()
    ap.open_server("com.makemind.sample.waiting")
    ap.wait_text("Front door")
    ap.tap("Counter →")
    ap.wait_text("Seat #42 Hughes")
    print("   counter sees the same", door_after.split(",")[0])
    ap.shot(f"{CAP}/02_counter.png")
    ap.tap("Seat #42 Hughes (4)")
    ap.wait_text("called")
    print("   counter:", [t for t, _ in ap.texts("called")][0])
    ap.shot(f"{CAP}/03_counter_after_call.png")
print("waiting-line: the door followed two calls, one from the screen and one from a second counter, untouched")
