"""Native Windows control surface for the Rezonance Local Connector."""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import threading
from typing import Any, Callable

from netcode import runner_agent


PRODUCT_NAME = "Rezonance Local Connector"
TASK_NAME = "RezonanceLocalConnector"
PLATFORMS = (
    ("Auto detect", ""),
    ("Cisco IOS / IOS-XE", "cisco_ios"),
    ("Cisco NX-OS", "cisco_nxos"),
    ("Arista EOS", "arista_eos"),
    ("Juniper Junos", "juniper_junos"),
    ("Fortinet FortiOS", "fortinet"),
    ("Palo Alto PAN-OS", "palo_alto"),
)


def connector_snapshot() -> dict[str, Any]:
    """Return UI-safe local state without identity or device secrets."""
    identity: dict[str, Any] = {}
    identity_error = ""
    if runner_agent.IDENTITY_FILE.exists():
        try:
            identity = runner_agent._load_identity()
        except BaseException as exc:  # SystemExit is used by the CLI loader.
            identity_error = str(exc)

    inventory_error = ""
    try:
        inventory = runner_agent._public_inventory_snapshot()
        devices = list(inventory.get("devices") or [])
    except Exception as exc:  # noqa: BLE001
        inventory_error = str(exc)
        devices = []

    return {
        "enrolled": bool(identity.get("runner_id")),
        "connector": {
            "name": str(identity.get("name") or os.environ.get("COMPUTERNAME") or "Windows connector"),
            "server": str(identity.get("server") or ""),
            "pool": str(identity.get("pool") or ""),
            "version": runner_agent.VERSION,
        },
        "inventory": {
            "configured": runner_agent.INVENTORY_FILE.exists() and not inventory_error,
            "device_count": len(devices),
            "devices": devices,
        },
        "security": {
            "dpapi": runner_agent.INVENTORY_FILE.suffix.lower() == ".dpapi",
            "outbound_only": True,
        },
        "errors": [value for value in (identity_error, inventory_error) if value],
    }


def _run_task(command: str) -> tuple[bool, str]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    completed = subprocess.run(
        ["schtasks.exe", f"/{command}", "/TN", TASK_NAME],
        capture_output=True,
        text=True,
        timeout=15,
        creationflags=flags,
        check=False,
    )
    message = (completed.stdout or completed.stderr or "").strip()
    return completed.returncode == 0, message


def main() -> int:
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except ImportError as exc:
        raise SystemExit(f"Windows UI components are unavailable: {exc}") from exc

    class ConnectorApp:
        def __init__(self) -> None:
            self.root = tk.Tk()
            self.root.title(PRODUCT_NAME)
            self.root.geometry("1040x720")
            self.root.minsize(900, 620)
            self.root.configure(bg="#f5f7f8")
            self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
            self.busy = False
            self._configure_style()
            self._build()
            self.refresh()
            self.root.after(120, self._drain_events)

        def _configure_style(self) -> None:
            style = ttk.Style(self.root)
            if "vista" in style.theme_names():
                style.theme_use("vista")
            style.configure("TFrame", background="#f5f7f8")
            style.configure("Panel.TFrame", background="#ffffff")
            style.configure("TLabel", background="#f5f7f8", foreground="#172126", font=("Segoe UI", 10))
            style.configure("Panel.TLabel", background="#ffffff", foreground="#172126", font=("Segoe UI", 10))
            style.configure("Title.TLabel", background="#15242b", foreground="#ffffff", font=("Segoe UI Semibold", 18))
            style.configure("Subtitle.TLabel", background="#15242b", foreground="#b9cbd2", font=("Segoe UI", 9))
            style.configure("Section.TLabel", background="#ffffff", foreground="#172126", font=("Segoe UI Semibold", 12))
            style.configure("Metric.TLabel", background="#ffffff", foreground="#172126", font=("Segoe UI Semibold", 22))
            style.configure("Muted.TLabel", background="#ffffff", foreground="#607078", font=("Segoe UI", 9))
            style.configure("Accent.TButton", font=("Segoe UI Semibold", 10), padding=(16, 8))
            style.configure("TButton", font=("Segoe UI", 10), padding=(12, 7))
            style.configure("TEntry", padding=6)
            style.configure("TCombobox", padding=5)
            style.configure("Treeview", rowheight=29, font=("Segoe UI", 9), background="#ffffff", fieldbackground="#ffffff")
            style.configure("Treeview.Heading", font=("Segoe UI Semibold", 9))
            style.configure("TNotebook", background="#f5f7f8", borderwidth=0)
            style.configure("TNotebook.Tab", font=("Segoe UI Semibold", 10), padding=(18, 9))

        def _build(self) -> None:
            header = tk.Frame(self.root, bg="#15242b", height=92)
            header.pack(fill="x")
            header.pack_propagate(False)
            title_box = tk.Frame(header, bg="#15242b")
            title_box.pack(side="left", padx=24, pady=14)
            ttk.Label(title_box, text=PRODUCT_NAME, style="Title.TLabel").pack(anchor="w")
            ttk.Label(title_box, text="Community | Windows", style="Subtitle.TLabel").pack(anchor="w")
            self.header_status = tk.Label(
                header,
                text="Checking",
                bg="#d99b3f",
                fg="#172126",
                font=("Segoe UI Semibold", 9),
                padx=12,
                pady=6,
            )
            self.header_status.pack(side="right", padx=24)

            body = ttk.Frame(self.root, padding=(22, 16, 22, 12))
            body.pack(fill="both", expand=True)
            self.tabs = ttk.Notebook(body)
            self.tabs.pack(fill="both", expand=True)
            self.overview_tab = ttk.Frame(self.tabs, style="Panel.TFrame", padding=20)
            self.discovery_tab = ttk.Frame(self.tabs, style="Panel.TFrame", padding=20)
            self.inventory_tab = ttk.Frame(self.tabs, style="Panel.TFrame", padding=20)
            self.tabs.add(self.overview_tab, text="Overview")
            self.tabs.add(self.discovery_tab, text="Discovery")
            self.tabs.add(self.inventory_tab, text="Inventory")
            self._build_overview()
            self._build_discovery()
            self._build_inventory()

            footer = tk.Frame(self.root, bg="#e8edef", height=38)
            footer.pack(fill="x")
            footer.pack_propagate(False)
            self.activity = tk.Label(
                footer,
                text="Ready",
                bg="#e8edef",
                fg="#43545c",
                font=("Segoe UI", 9),
                anchor="w",
            )
            self.activity.pack(fill="both", padx=22)

        def _build_overview(self) -> None:
            ttk.Label(self.overview_tab, text="Connector status", style="Section.TLabel").grid(
                row=0, column=0, columnspan=3, sticky="w", pady=(0, 14)
            )
            metrics = ttk.Frame(self.overview_tab, style="Panel.TFrame")
            metrics.grid(row=1, column=0, columnspan=3, sticky="ew")
            for column in range(3):
                metrics.columnconfigure(column, weight=1, uniform="metric")
            self.enrollment_value = self._metric(metrics, 0, "Enrollment")
            self.device_value = self._metric(metrics, 1, "Local devices")
            self.security_value = self._metric(metrics, 2, "Credential store")

            ttk.Separator(self.overview_tab).grid(row=2, column=0, columnspan=3, sticky="ew", pady=22)
            self.connection_panel = ttk.Frame(self.overview_tab, style="Panel.TFrame")
            self.connection_panel.grid(row=3, column=0, columnspan=3, sticky="ew")
            ttk.Label(self.connection_panel, text="Cloud connection", style="Section.TLabel").grid(
                row=0, column=0, columnspan=3, sticky="w", pady=(0, 12)
            )
            ttk.Label(self.connection_panel, text="Control plane", style="Muted.TLabel").grid(row=1, column=0, sticky="w")
            self.server_value = ttk.Label(self.connection_panel, text="Not enrolled", style="Panel.TLabel")
            self.server_value.grid(row=2, column=0, columnspan=2, sticky="w", pady=(2, 16))
            ttk.Label(self.connection_panel, text="Connector name", style="Muted.TLabel").grid(row=3, column=0, sticky="w")
            self.name_value = ttk.Label(self.connection_panel, text="-", style="Panel.TLabel")
            self.name_value.grid(row=4, column=0, sticky="w", pady=(2, 16))
            buttons = ttk.Frame(self.connection_panel, style="Panel.TFrame")
            buttons.grid(row=5, column=0, columnspan=3, sticky="w", pady=(8, 0))
            ttk.Button(buttons, text="Start connector", command=self._start_connector, style="Accent.TButton").pack(side="left")
            ttk.Button(buttons, text="Refresh", command=self.refresh).pack(side="left", padx=(10, 0))

            self.enrollment_panel = ttk.Frame(self.overview_tab, style="Panel.TFrame")
            self.enrollment_panel.grid(row=3, column=0, columnspan=3, sticky="ew")
            ttk.Label(self.enrollment_panel, text="Connect to Rezonance", style="Section.TLabel").grid(
                row=0, column=0, columnspan=4, sticky="w", pady=(0, 10)
            )
            self.enroll_server = tk.StringVar(value=os.getenv("NETCODE_CONTROL_PLANE_URL", ""))
            self.enroll_name = tk.StringVar(value=os.environ.get("COMPUTERNAME") or "windows-connector")
            self.enroll_token = tk.StringVar()
            self._field(self.enrollment_panel, 1, 0, "Control plane", self.enroll_server, columnspan=2)
            self._field(self.enrollment_panel, 1, 2, "Connector name", self.enroll_name, columnspan=2)
            ttk.Label(self.enrollment_panel, text="One-time join token", style="Muted.TLabel").grid(
                row=3, column=0, columnspan=2, sticky="w", pady=(10, 4)
            )
            ttk.Entry(self.enrollment_panel, textvariable=self.enroll_token, show="*").grid(
                row=4, column=0, columnspan=2, sticky="ew", padx=(0, 12)
            )
            self.enroll_button = ttk.Button(
                self.enrollment_panel,
                text="Enroll connector",
                command=self._enroll,
                style="Accent.TButton",
            )
            self.enroll_button.grid(row=4, column=2, sticky="w")
            for column in range(4):
                self.enrollment_panel.columnconfigure(column, weight=1, uniform="enroll")
            self.overview_tab.columnconfigure(0, weight=1)

        def _metric(self, parent: Any, column: int, label: str) -> Any:
            frame = ttk.Frame(parent, style="Panel.TFrame", padding=(0, 6, 22, 6))
            frame.grid(row=0, column=column, sticky="nsew")
            value = ttk.Label(frame, text="-", style="Metric.TLabel")
            value.pack(anchor="w")
            ttk.Label(frame, text=label, style="Muted.TLabel").pack(anchor="w")
            return value

        def _build_discovery(self) -> None:
            ttk.Label(self.discovery_tab, text="Discover local inventory", style="Section.TLabel").grid(
                row=0, column=0, columnspan=4, sticky="w", pady=(0, 16)
            )
            self.seed = tk.StringVar()
            self.allowed = tk.StringVar()
            self.excluded = tk.StringVar()
            self.site = tk.StringVar(value="default-site")
            self.platform_label = tk.StringVar(value=PLATFORMS[0][0])
            self.username = tk.StringVar()
            self.password = tk.StringVar()
            self.port = tk.IntVar(value=22)
            self.depth = tk.IntVar(value=1)
            self.concurrency = tk.IntVar(value=4)
            self.merge = tk.BooleanVar(value=True)

            self._field(self.discovery_tab, 1, 0, "Seed IP, range, or CIDR", self.seed, columnspan=2)
            self._field(self.discovery_tab, 1, 2, "Site", self.site, columnspan=2)
            self._field(self.discovery_tab, 3, 0, "Allowed CIDRs", self.allowed, columnspan=2)
            self._field(self.discovery_tab, 3, 2, "Excluded CIDRs", self.excluded, columnspan=2)

            ttk.Label(self.discovery_tab, text="Platform", style="Muted.TLabel").grid(row=5, column=0, sticky="w", pady=(10, 4))
            platform = ttk.Combobox(
                self.discovery_tab,
                textvariable=self.platform_label,
                values=[label for label, _ in PLATFORMS],
                state="readonly",
            )
            platform.grid(row=6, column=0, columnspan=2, sticky="ew", padx=(0, 12))
            self._field(self.discovery_tab, 5, 2, "SSH port", self.port, columnspan=1)
            self._field(self.discovery_tab, 5, 3, "Neighbor depth", self.depth, columnspan=1)

            self._field(self.discovery_tab, 7, 0, "Device username", self.username, columnspan=2)
            ttk.Label(self.discovery_tab, text="Device password", style="Muted.TLabel").grid(row=7, column=2, columnspan=2, sticky="w", pady=(10, 4))
            password = ttk.Entry(self.discovery_tab, textvariable=self.password, show="*")
            password.grid(row=8, column=2, columnspan=2, sticky="ew")

            settings = ttk.Frame(self.discovery_tab, style="Panel.TFrame")
            settings.grid(row=9, column=0, columnspan=4, sticky="ew", pady=(18, 10))
            ttk.Checkbutton(settings, text="Keep existing discovered devices", variable=self.merge).pack(side="left")
            ttk.Label(settings, text=f"Community limit: {runner_agent.COMMUNITY_MAX_DEVICES}", style="Muted.TLabel").pack(side="right")

            self.discover_button = ttk.Button(
                self.discovery_tab,
                text="Discover devices",
                command=self._discover,
                style="Accent.TButton",
            )
            self.discover_button.grid(row=10, column=0, sticky="w", pady=(8, 0))
            self.progress = ttk.Progressbar(self.discovery_tab, mode="indeterminate")
            self.progress.grid(row=10, column=1, columnspan=3, sticky="ew", padx=(16, 0), pady=(8, 0))
            self.progress.grid_remove()
            for column in range(4):
                self.discovery_tab.columnconfigure(column, weight=1, uniform="field")

        def _field(self, parent: Any, row: int, column: int, label: str, variable: Any, *, columnspan: int) -> None:
            ttk.Label(parent, text=label, style="Muted.TLabel").grid(
                row=row, column=column, columnspan=columnspan, sticky="w", pady=(10, 4)
            )
            ttk.Entry(parent, textvariable=variable).grid(
                row=row + 1,
                column=column,
                columnspan=columnspan,
                sticky="ew",
                padx=(0, 12 if column + columnspan < 4 else 0),
            )

        def _build_inventory(self) -> None:
            top = ttk.Frame(self.inventory_tab, style="Panel.TFrame")
            top.pack(fill="x", pady=(0, 12))
            ttk.Label(top, text="Discovered devices", style="Section.TLabel").pack(side="left")
            self.inventory_count = ttk.Label(top, text="0 devices", style="Muted.TLabel")
            self.inventory_count.pack(side="right")
            columns = ("device", "address", "platform", "site", "role")
            self.tree = ttk.Treeview(self.inventory_tab, columns=columns, show="headings", selectmode="browse")
            headings = {
                "device": "Device",
                "address": "Address",
                "platform": "Platform",
                "site": "Site",
                "role": "Role",
            }
            widths = {"device": 190, "address": 150, "platform": 160, "site": 150, "role": 120}
            for column in columns:
                self.tree.heading(column, text=headings[column])
                self.tree.column(column, width=widths[column], minwidth=90, stretch=True)
            scrollbar = ttk.Scrollbar(self.inventory_tab, orient="vertical", command=self.tree.yview)
            self.tree.configure(yscrollcommand=scrollbar.set)
            self.tree.pack(side="left", fill="both", expand=True)
            scrollbar.pack(side="right", fill="y")

        @staticmethod
        def _csv(value: str) -> list[str]:
            return [item.strip() for item in value.split(",") if item.strip()]

        def _set_busy(self, value: bool, message: str) -> None:
            self.busy = value
            self.discover_button.configure(state="disabled" if value else "normal")
            if value:
                self.progress.grid()
                self.progress.start(12)
            else:
                self.progress.stop()
                self.progress.grid_remove()
            self.activity.configure(text=message)

        def _background(self, function: Callable[[], Any], event: str) -> None:
            def worker() -> None:
                try:
                    self.events.put((event, function()))
                except Exception as exc:  # noqa: BLE001
                    self.events.put(("error", f"{type(exc).__name__}: {exc}"))

            threading.Thread(target=worker, daemon=True).start()

        def _discover(self) -> None:
            if self.busy:
                return
            platform_value = dict(PLATFORMS).get(self.platform_label.get(), "")
            payload = {
                "seed_node": self.seed.get().strip(),
                "allowed_cidrs": self._csv(self.allowed.get()),
                "excluded_cidrs": self._csv(self.excluded.get()),
                "site": self.site.get().strip(),
                "platform": platform_value,
                "port": self.port.get(),
                "username": self.username.get().strip(),
                "password": self.password.get(),
                "depth": self.depth.get(),
                "max_devices": runner_agent.COMMUNITY_MAX_DEVICES,
                "concurrency": self.concurrency.get(),
                "merge": self.merge.get(),
            }
            if not payload["seed_node"] or not payload["username"] or not payload["password"]:
                messagebox.showerror(PRODUCT_NAME, "Seed, username, and password are required.")
                return
            self.password.set("")
            self._set_busy(True, "Validating discovery scope")

            def progress(event: dict[str, Any]) -> None:
                self.events.put(("progress", str(event.get("message") or "Discovering devices")))

            self._background(
                lambda: runner_agent.bootstrap_discovered_inventory(payload, progress),
                "discovery_complete",
            )

        def _enroll(self) -> None:
            server = self.enroll_server.get().strip().rstrip("/")
            token = self.enroll_token.get().strip()
            name = self.enroll_name.get().strip()
            if not server.startswith("https://"):
                messagebox.showerror(PRODUCT_NAME, "A production control plane must use HTTPS.")
                return
            if not token or not name:
                messagebox.showerror(PRODUCT_NAME, "Connector name and one-time join token are required.")
                return
            self.enroll_token.set("")
            self.enroll_button.configure(state="disabled")
            self.activity.configure(text="Enrolling connector")
            self._background(
                lambda: runner_agent.enroll(argparse.Namespace(server=server, join_token=token, name=name)),
                "enrollment_complete",
            )

        def _start_connector(self) -> None:
            self.activity.configure(text="Starting connector")
            self._background(lambda: _run_task("Run"), "task_complete")

        def _drain_events(self) -> None:
            try:
                while True:
                    event, value = self.events.get_nowait()
                    if event == "progress":
                        self.activity.configure(text=str(value))
                    elif event == "discovery_complete":
                        self._set_busy(False, "Discovery complete" if value.get("ok") else "Discovery failed")
                        self.refresh()
                        if value.get("ok"):
                            count = int((value.get("inventory") or {}).get("discovered") or 0)
                            messagebox.showinfo(PRODUCT_NAME, f"Discovered and protected {count} device record(s).")
                            self.tabs.select(self.inventory_tab)
                        else:
                            messagebox.showerror(PRODUCT_NAME, str(value.get("error") or "Discovery failed."))
                    elif event == "task_complete":
                        ok, message = value
                        self.activity.configure(text="Connector started" if ok else "Connector could not start")
                        if not ok:
                            messagebox.showerror(PRODUCT_NAME, message or "The startup task is not installed.")
                    elif event == "enrollment_complete":
                        self.enroll_button.configure(state="normal")
                        if int(value) == 0:
                            self.activity.configure(text="Connector enrolled")
                            self.refresh()
                            self.tabs.select(self.discovery_tab)
                        else:
                            self.activity.configure(text="Enrollment failed")
                            messagebox.showerror(PRODUCT_NAME, "Enrollment failed. Confirm the URL and one-time token.")
                    elif event == "error":
                        self._set_busy(False, "Operation failed")
                        messagebox.showerror(PRODUCT_NAME, str(value))
            except queue.Empty:
                pass
            self.root.after(120, self._drain_events)

        def refresh(self) -> None:
            snapshot = connector_snapshot()
            enrolled = bool(snapshot["enrolled"])
            devices = list(snapshot["inventory"]["devices"])
            ready = enrolled and bool(devices) and not snapshot["errors"]
            self.header_status.configure(
                text="Ready" if ready else ("Inventory needed" if enrolled else "Enrollment needed"),
                bg="#55b58a" if ready else "#d99b3f",
                fg="#ffffff" if ready else "#172126",
            )
            self.enrollment_value.configure(text="Active" if enrolled else "Needed")
            self.device_value.configure(text=str(len(devices)))
            self.security_value.configure(text="DPAPI" if snapshot["security"]["dpapi"] else "Local file")
            self.server_value.configure(text=snapshot["connector"]["server"] or "Not enrolled")
            self.name_value.configure(text=snapshot["connector"]["name"])
            if enrolled:
                self.enrollment_panel.grid_remove()
                self.connection_panel.grid()
            else:
                self.connection_panel.grid_remove()
                self.enrollment_panel.grid()
            self.inventory_count.configure(text=f"{len(devices)} device{'s' if len(devices) != 1 else ''}")
            self.tabs.tab(self.discovery_tab, state="normal" if enrolled else "disabled")
            for item in self.tree.get_children():
                self.tree.delete(item)
            for device in sorted(devices, key=lambda item: str(item.get("hostname") or item.get("id") or "").lower()):
                self.tree.insert("", "end", values=(
                    device.get("hostname") or device.get("id") or "",
                    device.get("host") or "",
                    device.get("platform") or "",
                    device.get("site") or "",
                    device.get("role") or "",
                ))
            if snapshot["errors"]:
                self.activity.configure(text=str(snapshot["errors"][0]))

        def run(self) -> None:
            self.root.mainloop()

    ConnectorApp().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
