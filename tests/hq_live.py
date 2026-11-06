"""Reaper HQ running for real (uvicorn on 127.0.0.1, its own port) against a throwaway Town, for browser tests.

The Town is the Reaper test set's frozen fake Town with a scripted model: no network, no real API, no real
Cloudflare or Gmail. The server can be stopped and started again on the same port and token, to test that the
page's live stream reconnects by itself.
"""
import socket
import threading
import time
from pathlib import Path
from typing import Any

import uvicorn

from engine import hq
from engine.scripted_llm import ScriptedClient
from packs.websites import testset_reaper as frozen
from packs.websites.townview import VIEW

TOKEN = "fictional-page-session"


class LiveHQ:
    def __init__(self, folder: Path, client: ScriptedClient | None = None, token: str = TOKEN,
                 rules: Any = None) -> None:
        self.rules = rules        # engine.decisions.Rules (orders, "go"); None = the default (no orders)
        self.folder = folder
        self.client = client or ScriptedClient()
        self.token = token
        self.port = hq.free_port()
        frozen.open_fake_town(folder, client=self.client).close()   # seed once
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None
        # HQ's timing log (engine/hq.py create_app's `timing`), seconds since start: a failing page test prints it.
        self.started = time.monotonic()
        self.timings: list[str] = []

    def note(self, line: str) -> None:
        self.timings.append(f"{time.monotonic() - self.started:7.2f}s  {line}")
        del self.timings[:-400]   # the last 400 lines are plenty, even for the 10-minute stress test

    def timing_report(self) -> str:
        return "HQ timing log (seconds since the server started):\n" + "\n".join(self.timings)

    def open_town(self) -> Any:
        return frozen.open_fake_town(self.folder, client=self.client)

    @property
    def url(self) -> str:
        return f"http://{hq.HOST}:{self.port}/?token={self.token}"

    @property
    def lite_url(self) -> str:
        """The page with lighter graphics (lower resolution, no shadows): the browser tests use it, so they run in
        reasonable time on a machine where the browser draws 3D in software."""
        return self.url + "&lite=1"

    def start(self) -> "LiveHQ":
        extra = {"rules": self.rules} if self.rules is not None else {}
        app = hq.create_app(self.open_town, VIEW, self.token, self.port, can_decide=True, timing=self.note, **extra)
        config = uvicorn.Config(app, host=hq.HOST, port=self.port, log_level="critical", access_log=False,
                                timeout_graceful_shutdown=1)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("HQ didn't start")

    def stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout=10)
        # Wait until the port is free again, so a restart can use it.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                if sock.connect_ex((hq.HOST, self.port)) != 0:
                    return
            time.sleep(0.1)
