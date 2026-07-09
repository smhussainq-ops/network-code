"""Interactive PTY bridge — runs on the RUNNER, next to the device.

Opens a real SSH shell channel (paramiko invoke_shell) and bridges it to an
upstream message stream. When guard mode is enabled, every byte of engineer
INPUT defaults to a direct live SSH terminal while command lines are still
emitted for the evidence transcript. Optional guard mode can still prompt or
block selected risky input at Enter. Device OUTPUT streams back upstream in real
time (the read direction).

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

    def __init__(
        self,
        device: Any,
        state: ShellSessionState,
        on_output: OnOutput,
        on_event: OnEvent,
        *,
        guard_enabled: bool = False,
    ):
        self.device = device
        self.state = state
        self.on_output = on_output
        self.on_event = on_event
        self.guard_enabled = guard_enabled
        self._direct_line = ""
        self._direct_escape_state = ""
        self._direct_history: list[str] = []
        self._direct_history_index: int | None = None
        self._client = None
        self._chan = None
        self._stop = False
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()

    def open(self, term: str = "xterm", width: int = 120, height: int = 40, timeout: int = 20) -> None:
        import paramiko
        from netcode.adapters.shell import ssh_port_for

        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._client.connect(
            self.device.host,
            port=ssh_port_for(self.device),
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
        self.on_event({
            "type": "session",
            "action": "opened",
            "device_id": getattr(self.device, "id", ""),
            "guard_enabled": self.guard_enabled,
        })
        if not self.guard_enabled:
            self.on_event({
                "type": "guard",
                "action": "direct_mode",
                "guard_enabled": False,
                "message": "Direct CLI mode active. Governance blocking is disabled; commands are still logged.",
            })

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
        """Engineer input: guard it when enabled; otherwise pass it through."""
        forward = text
        if self.guard_enabled:
            with self._lock:
                decision = feed(self.state, text)
            for event in decision.events:
                self.on_event(event)
            forward = decision.forward
        else:
            self._record_direct_input(text)

        if forward and self._chan is not None:
            try:
                self._chan.send(forward)
            except Exception as exc:  # noqa: BLE001
                self.on_event({"type": "session", "action": "error", "message": str(exc)})

    def _record_direct_input(self, text: str) -> None:
        """Best-effort command capture for raw SSH mode.

        This does not gate or rewrite input. It only reconstructs line-oriented
        commands so the control plane can produce a useful session audit trail.
        """
        for char in text:
            if self._direct_escape_state == "escape":
                self._direct_escape_state = "csi" if char in ("[", "O") else ""
                continue
            if self._direct_escape_state == "csi":
                if "@" <= char <= "~":
                    if char == "A" and self._direct_history:
                        if self._direct_history_index is None:
                            self._direct_history_index = len(self._direct_history) - 1
                        else:
                            self._direct_history_index = max(0, self._direct_history_index - 1)
                        self._direct_line = self._direct_history[self._direct_history_index]
                    elif char == "B" and self._direct_history_index is not None:
                        if self._direct_history_index < len(self._direct_history) - 1:
                            self._direct_history_index += 1
                            self._direct_line = self._direct_history[self._direct_history_index]
                        else:
                            self._direct_history_index = None
                            self._direct_line = ""
                    self._direct_escape_state = ""
                continue
            if char == "\x1b":
                self._direct_escape_state = "escape"
                continue
            if char in ("\r", "\n"):
                line = self._direct_line.strip()
                self._direct_line = ""
                self._direct_history_index = None
                if line:
                    self._direct_history.append(line)
                    self._update_direct_state(line)
                    self.on_event({
                        "type": "command",
                        "action": "direct_command",
                        "line": line,
                        "kind": "direct",
                        "change_id": self.state.change_id,
                        "mode": self.state.mode,
                        "in_config": self.state.in_config,
                        "device_touched": self.state.device_touched,
                    })
            elif char in ("\b", "\x7f"):
                self._direct_history_index = None
                self._direct_line = self._direct_line[:-1]
            elif char == "\x03":
                self._direct_line = ""
                self._direct_history_index = None
            elif char.isprintable():
                self._direct_history_index = None
                self._direct_line += char

    def _update_direct_state(self, line: str) -> None:
        normalized = " ".join(line.lower().split())
        if normalized in ("configure terminal", "conf t", "configure"):
            self.state.in_config = True
            return
        elif normalized in ("end", "disable"):
            self.state.in_config = False
            return
        elif normalized == "exit" and self.state.in_config:
            return
        elif self.state.in_config and normalized not in ("exit", "end") and not normalized.startswith(("show ", "do show ")):
            self.state.device_touched = True

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
