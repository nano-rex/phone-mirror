"""Thin wrapper around the adb command line tool."""

import os
import shutil
import subprocess


class AdbError(RuntimeError):
    pass


def find_adb():
    path = os.environ.get("ADB") or shutil.which("adb")
    if not path:
        raise AdbError(
            "adb not found. Install Android platform-tools "
            "(https://developer.android.com/tools/releases/platform-tools) "
            "and put adb on PATH, or set the ADB environment variable.")
    return path


def parse_devices(output):
    """Parse `adb devices -l` output into a list of (serial, state, description)."""
    devices = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        info = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
        devices.append((serial, state, info.get("model", "").replace("_", " ")))
    return devices


class Adb:
    def __init__(self, serial=None, path=None):
        self.path = path or find_adb()
        self.serial = serial

    def _base(self):
        cmd = [self.path]
        if self.serial:
            cmd += ["-s", self.serial]
        return cmd

    def run(self, *args, check=True, timeout=30):
        proc = subprocess.run(self._base() + list(args), capture_output=True,
                              text=True, timeout=timeout)
        if check and proc.returncode != 0:
            msg = (proc.stderr or proc.stdout).strip()
            raise AdbError(f"adb {' '.join(args)} failed: {msg}")
        return proc.stdout

    def popen(self, *args, **kwargs):
        return subprocess.Popen(self._base() + list(args), **kwargs)

    def devices(self):
        return parse_devices(subprocess.run([self.path, "devices", "-l"], capture_output=True,
                                            text=True, timeout=30).stdout)

    def connect(self, address):
        out = subprocess.run([self.path, "connect", address], capture_output=True,
                             text=True, timeout=30).stdout.strip()
        if "connected" not in out:
            raise AdbError(f"adb connect {address}: {out}")
        return out

    def select_device(self):
        """Pick the serial to use, failing clearly when it is ambiguous."""
        ready = [d for d in self.devices() if d[1] == "device"]
        if self.serial:
            if not any(d[0] == self.serial for d in ready):
                raise AdbError(f"device {self.serial} is not connected or not authorised")
            return self.serial
        if not ready:
            others = [f"{s} ({st})" for s, st, _ in self.devices()]
            hint = f" Found: {', '.join(others)}." if others else ""
            raise AdbError("no Android device ready. Enable USB debugging and accept "
                           "the prompt on the phone." + hint)
        if len(ready) > 1:
            raise AdbError("several Android devices connected, choose one with --serial: "
                           + ", ".join(d[0] for d in ready))
        self.serial = ready[0][0]
        return self.serial

    def push(self, local, remote):
        self.run("push", local, remote, timeout=120)

    def forward(self, local_port, remote):
        self.run("forward", f"tcp:{local_port}", remote)

    def forward_remove(self, local_port):
        self.run("forward", "--remove", f"tcp:{local_port}", check=False)
