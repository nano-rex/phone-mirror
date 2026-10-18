"""Computer side for the Android app's interop unit test (run by CI).

Usage: python tests/interop_server.py PORT
Accepts one connection with the fixed test pairing, echoes one record, then exits 0.
"""

import socket
import sys

from phonemirror.link.secure import server_handshake

PAIR_ID = bytes(range(16))
KEY = bytes(range(32))


def main(port):
    with socket.create_server(("127.0.0.1", port)) as server:
        server.settimeout(600)
        print("ready", flush=True)
        conn, _ = server.accept()
        with conn:
            channel, _ = server_handshake(conn, lambda pid: KEY if pid == PAIR_ID else None)
            channel.send(b"echo:" + channel.recv())
    print("interop ok", flush=True)


if __name__ == "__main__":
    main(int(sys.argv[1]))
