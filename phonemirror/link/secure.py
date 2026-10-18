"""Authenticated, encrypted channel between the phone app and the computer.

See docs/protocol.md. The phone is the client, the computer the server.
"""

import hashlib
import hmac
import os
import struct

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"PMIR"
VERSION = 1
MAX_RECORD = 8 * 1024 * 1024
MAX_KEY_DER = 512


class HandshakeError(ConnectionError):
    pass


def recv_exact(sock, size):
    buf = bytearray()
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        if not chunk:
            raise ConnectionError("connection closed")
        buf += chunk
    return bytes(buf)


def _hmac(key, data):
    return hmac.new(key, data, hashlib.sha256).digest()


def hkdf_expand(prk, info, length):
    out, block, i = b"", b"", 1
    while len(out) < length:
        block = _hmac(prk, block + info + bytes([i]))
        out += block
        i += 1
    return out[:length]


def derive_keys(psk, shared, transcript):
    th = hashlib.sha256(transcript).digest()
    prk = _hmac(psk, shared)
    okm = hkdf_expand(prk, b"pm-keys" + th, 64)
    return okm[:32], okm[32:]


def transcript_mac(psk, label, transcript):
    return _hmac(psk, label + hashlib.sha256(transcript).digest())


def _ephemeral():
    priv = ec.generate_private_key(ec.SECP256R1())
    der = priv.public_key().public_bytes(serialization.Encoding.DER,
                                         serialization.PublicFormat.SubjectPublicKeyInfo)
    return priv, der


def _exchange(priv, peer_der):
    peer = serialization.load_der_public_key(peer_der)
    if not isinstance(peer, ec.EllipticCurvePublicKey) or peer.curve.name != "secp256r1":
        raise HandshakeError("peer key is not P-256")
    return priv.exchange(ec.ECDH(), peer)


class Channel:
    """Record layer: AES-256-GCM with counter nonces, one key per direction."""

    def __init__(self, sock, send_key, recv_key):
        self.sock = sock
        self._send = AESGCM(send_key)
        self._recv = AESGCM(recv_key)
        self._send_ctr = 0
        self._recv_ctr = 0

    @staticmethod
    def _nonce(counter):
        return b"\0\0\0\0" + struct.pack(">Q", counter)

    def seal(self, plaintext):
        ct = self._send.encrypt(self._nonce(self._send_ctr), plaintext, None)
        self._send_ctr += 1
        return struct.pack(">I", len(ct)) + ct

    def send(self, plaintext):
        self.sock.sendall(self.seal(plaintext))

    def recv(self):
        (length,) = struct.unpack(">I", recv_exact(self.sock, 4))
        if length < 16 or length > MAX_RECORD:
            raise ConnectionError(f"bad record length {length}")
        ct = recv_exact(self.sock, length)
        try:
            pt = self._recv.decrypt(self._nonce(self._recv_ctr), ct, None)
        except Exception:
            raise ConnectionError("record authentication failed") from None
        self._recv_ctr += 1
        return pt


def server_handshake(sock, lookup_key):
    """Run the computer side. lookup_key(pair_id: bytes) -> psk or None.

    Returns (channel, pair_id).
    """
    head = recv_exact(sock, 4 + 1 + 16 + 32 + 2)
    if head[:4] != MAGIC:
        raise HandshakeError("not a phonemirror client")
    if head[4] != VERSION:
        raise HandshakeError(f"unsupported protocol version {head[4]}")
    pair_id = head[5:21]
    (klen,) = struct.unpack(">H", head[53:55])
    if not 0 < klen <= MAX_KEY_DER:
        raise HandshakeError("bad key length")
    epk_c = recv_exact(sock, klen)
    client_hello = head + epk_c
    psk = lookup_key(pair_id)
    if psk is None:
        raise HandshakeError("unknown pairing (pair the phone again)")

    priv, epk_s = _ephemeral()
    server_hello = MAGIC + bytes([VERSION]) + os.urandom(32) + struct.pack(">H", len(epk_s)) + epk_s
    transcript = client_hello + server_hello
    sock.sendall(server_hello + transcript_mac(psk, b"pm-server", transcript))
    mac_c = recv_exact(sock, 32)
    if not hmac.compare_digest(mac_c, transcript_mac(psk, b"pm-client", transcript)):
        raise HandshakeError("phone failed authentication")
    shared = _exchange(priv, epk_c)
    k_c2s, k_s2c = derive_keys(psk, shared, transcript)
    return Channel(sock, send_key=k_s2c, recv_key=k_c2s), pair_id


def client_handshake(sock, pair_id, psk):
    """Run the phone side (used by tests and as a reference for the app)."""
    priv, epk_c = _ephemeral()
    client_hello = (MAGIC + bytes([VERSION]) + pair_id + os.urandom(32)
                    + struct.pack(">H", len(epk_c)) + epk_c)
    sock.sendall(client_hello)
    head = recv_exact(sock, 4 + 1 + 32 + 2)
    if head[:4] != MAGIC or head[4] != VERSION:
        raise HandshakeError("not a phonemirror server")
    (klen,) = struct.unpack(">H", head[37:39])
    if not 0 < klen <= MAX_KEY_DER:
        raise HandshakeError("bad key length")
    epk_s = recv_exact(sock, klen)
    server_hello = head + epk_s
    transcript = client_hello + server_hello
    mac_s = recv_exact(sock, 32)
    if not hmac.compare_digest(mac_s, transcript_mac(psk, b"pm-server", transcript)):
        raise HandshakeError("computer failed authentication")
    sock.sendall(transcript_mac(psk, b"pm-client", transcript))
    shared = _exchange(priv, epk_s)
    k_c2s, k_s2c = derive_keys(psk, shared, transcript)
    return Channel(sock, send_key=k_c2s, recv_key=k_s2c)
