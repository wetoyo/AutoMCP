"""Runtime bridge: attach Frida to an authorized process and run a saved adapter.

The bridge only executes adapters from the adapter store; it never generates code.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

import frida

ADAPTERS_DIR = Path(__file__).resolve().parent.parent / "adapters"
WAM_LIB = Path(__file__).resolve().parent / "wam_lib.js"
_HEX = re.compile(r"^0x[0-9a-fA-F]+$")


def _ints(value):
    """adapter_config values written as "0x..." strings become integers for the agent."""
    if isinstance(value, dict):
        return {k: _ints(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_ints(v) for v in value]
    if isinstance(value, str) and _HEX.match(value):
        return int(value, 16)
    return value


@dataclass
class Adapter:
    path: Path
    manifest: dict

    @classmethod
    def load(cls, app: str, operation: str) -> "Adapter":
        return cls.from_dir(ADAPTERS_DIR / app / operation)

    @classmethod
    def from_dir(cls, path: Path) -> "Adapter":
        path = Path(path)
        return cls(path, json.loads((path / "manifest.json").read_text(encoding="utf-8")))

    def agent_source(self) -> str:
        app = self.manifest["app"]
        config = {"module": app["module"], "pe_timestamp": int(app["pe_timestamp"], 16), "timeout_ms": 5000,
                  **_ints(self.manifest.get("adapter_config", {}))}
        js = (self.path / "adapter.js").read_text(encoding="utf-8")
        return f"const CONFIG = {json.dumps(config)};\n{WAM_LIB.read_text(encoding='utf-8')}\n{js}"


class BridgeError(RuntimeError):
    pass


def _kernel32():
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    return ctypes, wintypes, k32


def process_image_path(pid: int) -> Path:
    ctypes, wintypes, k32 = _kernel32()
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        raise BridgeError(f"cannot open pid {pid}: winerror {ctypes.get_last_error()}")
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buf))
        if not k32.QueryFullProcessImageNameW(wintypes.HANDLE(h), 0, buf, ctypes.byref(size)):
            raise BridgeError(f"cannot query image of pid {pid}: winerror {ctypes.get_last_error()}")
    finally:
        k32.CloseHandle(wintypes.HANDLE(h))
    return Path(buf.value)


def process_image_sha256(pid: int) -> str:
    """Hash the on-disk image of the target process (identity check before attaching code)."""
    return hashlib.sha256(process_image_path(pid).read_bytes()).hexdigest()


def pid_alive(pid: int) -> bool:
    ctypes, wintypes, k32 = _kernel32()
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return False
    try:
        code = wintypes.DWORD()
        return bool(k32.GetExitCodeProcess(wintypes.HANDLE(h), ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
    finally:
        k32.CloseHandle(wintypes.HANDLE(h))


def find_pids(image_name: str) -> list[int]:
    return [p.pid for p in frida.get_local_device().enumerate_processes() if p.name.lower() == image_name.lower()]


class Session:
    """One Frida session + loaded adapter script against one pid."""

    def __init__(self, pid: int, adapter: Adapter, verify_identity: bool = True):
        self.pid = pid
        self.adapter = adapter
        if verify_identity:
            got = process_image_sha256(pid)
            want = adapter.manifest["app"]["sha256"]
            if got != want:
                raise BridgeError(f"pid {pid} image sha256 {got} does not match adapter's {want}")
        self._session = frida.attach(pid)
        self._detached_reason: str | None = None
        self._session.on("detached", self._on_detached)
        self._script = self._session.create_script(adapter.agent_source())
        self._script.load()

    def _on_detached(self, reason, crash):
        self._detached_reason = f"{reason}" + (f" crash={crash.summary}" if crash else "")

    @property
    def alive(self) -> bool:
        return self._detached_reason is None

    def check(self) -> dict:
        return self._script.exports_sync.check()

    def invoke(self, args: dict | None = None) -> dict:
        if not self.alive:
            raise BridgeError(f"session detached: {self._detached_reason}")
        try:
            return self._script.exports_sync.invoke(args or {})
        except frida.InvalidOperationError as e:
            raise BridgeError(f"target detached during call: {self._detached_reason or e}") from e

    def close(self):
        try:
            self._script.unload()
            self._session.detach()
        except frida.InvalidOperationError:
            pass
