"""Interactive PTY bridge — runs on the RUNNER, next to the device.

Opens a real SSH shell channel (paramiko invoke_shell) and bridges it to an
upstream message stream. Every byte of engineer INPUT is run through the
streaming shell guard (shell_guard.feed) before it can reach the device:
config mode is blocked until a change is attached, credential/dangerous/unknown
commands are killed at Enter, pastes are staged, history-recall taints the
line. Device OUTPUT streams back upstream in real time (the read direction).

The whole session — every guarded input decision and a size-bounded sample of
output — is emitted as the evidence transcript. Credentials never leave the
customer network: paramiko resolves them from the runner's LOCAL inventory.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from netcode.shell_guard import ShellSessionState, feed

OnOutput = Callable[[bytes], None]
OnEvent = Callable[[dict[str, Any]], None]


class InteractivePtySession:
    """One live, guarded SSH shell to a device. Thread-safe for a single reader
    thread (device -> upstream) plus caller-driven writes (upstream -> device)."""

    def __init__(self, device: Any, state: ShellSessionState, on_output: OnOutput, on_event: OnEvent):
        self.device = device
        self.state = state
        self.on_output = on_output
        self.on_event = on_event
        self._client = None
        self._chan = None
        self._stop = False
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()

    def open(self, term: str = "xterm", width: int = 120, height: int = 40, timeout: int = 20) -> None:
        import paramiko

        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._client.connect(
            self.device.host,
            port=int(getattr(self.device, "port", 22) or 22),
            username=self.device.username,
            password=self.device.password,
            look_for_keys=False,
            allow_agent=False,
            timeout=timeout,
        )
        self._chan = self._client.invoke_shell(term=term, width=width, height=height)
        self._chan.settimeout(0.0)
        self._reader = threading.Thread(target=self._read_loop, name="pty-reader", daemon=True)
        self._reader.start()
        self.on_event({"type": "session", "action": "opened", "device_id": getattr(self.device, "id", "")})

    def _read_loop(self) -> None:
        while not self._stop:
            try:
                if self._chan is not None and self._chan.recv_ready():
                    data = self._chan.recv(8192)
                    if not data:
                        break
                    self.on_output(data)
                else:
                    time.sleep(0.02)
            except Exception:  # noqa: BLE001
                break
        self._stop = True
        self.on_event({"type": "session", "action": "closed"})

    def write(self, text: str) -> None:
        """Engineer input: run it through the guard, then send only what clears."""
        with self._lock:
            decision = feed(self.state, text)
        for event in decision.events:
            self.on_event(event)
        if decision.forward and self._chan is not None:
            try:
                self._chan.send(decision.forward)
            except Exception as exc:  # noqa: BLE001
                self.on_event({"type": "session", "action": "error", "message": str(exc)})

    def resize(self, width: int, height: int) -> None:
        try:
            if self._chan is not None:
                self._chan.resize_pty(width=max(20, int(width)), height=max(5, int(height)))
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        self._stop = True
        try:
            if self._chan is not None:
                self._chan.close()
        finally:
            if self._client is not None:
                self._client.close()

    @property
    def device_touched(self) -> bool:
        return self.state.device_touched
