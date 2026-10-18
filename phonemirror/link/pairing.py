"""Pairings between this computer and phones, stored in the user's config directory."""

import base64
import json
import os
import secrets
import socket
import sys
import urllib.parse
from dataclasses import dataclass

DEFAULT_PORT = 27199


def config_dir():
    if os.environ.get("PHONEMIRROR_CONFIG"):
        return os.environ["PHONEMIRROR_CONFIG"]
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "phonemirror")


def b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def local_addresses():
    """IPv4 addresses the phone may reach this computer on (LAN first)."""
    found = []
    try:
        # no packet is sent: this only asks the OS which interface routes outward
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addr = info[4][0]
            if addr not in found and not addr.startswith("127."):
                found.append(addr)
    except OSError:
        pass
    return found


@dataclass
class Pairing:
    pair_id: bytes
    key: bytes
    name: str = ""  # phone name, filled in after the first connection

    def to_json(self):
        return {"key": b64u(self.key), "name": self.name}


class PairingStore:
    def __init__(self, directory=None):
        self.directory = directory or config_dir()
        self.path = os.path.join(self.directory, "pairings.json")
        self._data = None

    def _load(self):
        if self._data is None:
            try:
                with open(self.path, encoding="utf-8") as f:
                    self._data = json.load(f)
            except (OSError, ValueError):
                self._data = {}
            self._data.setdefault("pairings", {})
            if "pc_id" not in self._data:
                self._data["pc_id"] = secrets.token_hex(16)
                self._save()
        return self._data

    def _save(self):
        os.makedirs(self.directory, exist_ok=True)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)
        os.replace(tmp, self.path)

    @property
    def pc_id(self):
        return self._load()["pc_id"]

    def pairings(self):
        return [Pairing(bytes.fromhex(pid), unb64u(p["key"]), p.get("name", ""))
                for pid, p in self._load()["pairings"].items()]

    def get(self, pair_id):
        p = self._load()["pairings"].get(pair_id.hex())
        return Pairing(pair_id, unb64u(p["key"]), p.get("name", "")) if p else None

    def add(self, pairing):
        self._load()["pairings"][pairing.pair_id.hex()] = pairing.to_json()
        self._save()

    def remove(self, pair_id):
        if self._load()["pairings"].pop(pair_id.hex(), None) is not None:
            self._save()
            return True
        return False

    def set_name(self, pair_id, name):
        p = self._load()["pairings"].get(pair_id.hex())
        if p is not None and p.get("name") != name:
            p["name"] = name
            self._save()


def new_pairing():
    return Pairing(secrets.token_bytes(16), secrets.token_bytes(32))


def pairing_uri(pairing, pc_id, pc_name, hosts, port=DEFAULT_PORT):
    query = urllib.parse.urlencode({
        "v": "1",
        "id": pairing.pair_id.hex(),
        "k": b64u(pairing.key),
        "pc": pc_id,
        "n": pc_name[:64],
        "h": ",".join(hosts),
        "p": str(port),
    })
    return f"phonemirror://pair?{query}"


def parse_pairing_uri(uri):
    parsed = urllib.parse.urlsplit(uri)
    if parsed.scheme != "phonemirror" or parsed.netloc != "pair":
        raise ValueError("not a phonemirror pairing link")
    q = dict(urllib.parse.parse_qsl(parsed.query))
    if q.get("v") != "1":
        raise ValueError("unsupported pairing version")
    pair_id, key = bytes.fromhex(q["id"]), unb64u(q["k"])
    if len(pair_id) != 16 or len(key) != 32:
        raise ValueError("malformed pairing")
    return {"pairing": Pairing(pair_id, key), "pc": q["pc"], "name": q.get("n", ""),
            "hosts": [h for h in q.get("h", "").split(",") if h], "port": int(q.get("p", DEFAULT_PORT))}


def computer_name():
    return (socket.gethostname().split(".")[0] or "computer")[:64]
