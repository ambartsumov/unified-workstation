"""The daemon's control channel.

POSIX: a Unix socket readable only by its owner (mode 0600) — the file permission is the
authentication. Windows: a loopback TCP port chosen by the system plus a random token; both
are written to a file inside the user's profile, so only that user can find and use them.
Nothing here ever listens on a network interface.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets as pysecrets
import socket
from pathlib import Path

from . import paths

UNIX = hasattr(socket, "AF_UNIX") and os.name != "nt"


def endpoint_file() -> Path:
    return paths.runtime_dir() / "suwd.endpoint"


def available() -> bool:
    return paths.socket_path().exists() if UNIX else endpoint_file().exists()


async def start_server(handler):
    """Start listening. Returns (server, cleanup). `handler(reader, writer, authorised)`."""
    paths.ensure(paths.runtime_dir())
    if UNIX:
        sock = paths.socket_path()
        sock.unlink(missing_ok=True)
        old_umask = os.umask(0o177)
        try:
            server = await asyncio.start_unix_server(lambda r, w: handler(r, w, True), path=str(sock))
        finally:
            os.umask(old_umask)
        return server, lambda: sock.unlink(missing_ok=True)

    token = pysecrets.token_urlsafe(32)

    async def guarded(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            first = await asyncio.wait_for(reader.readline(), timeout=5)
        except (asyncio.TimeoutError, OSError):
            first = b""
        await handler(reader, writer, pysecrets.compare_digest(first.strip(), token.encode()))

    server = await asyncio.start_server(guarded, host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    file = endpoint_file()
    file.write_text(json.dumps({"port": port, "token": token}))
    return server, lambda: file.unlink(missing_ok=True)


def request(payload: dict, timeout: float = 5) -> dict | None:
    """One request, one reply. None when the daemon is not running or does not answer."""
    try:
        if UNIX:
            path = paths.socket_path()
            if not path.exists():
                return None
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(timeout)
            client.connect(str(path))
            preamble = b""
        else:
            spec = json.loads(endpoint_file().read_text())
            client = socket.create_connection(("127.0.0.1", int(spec["port"])), timeout=timeout)
            preamble = str(spec["token"]).encode() + b"\n"
        with client:
            client.sendall(preamble + (json.dumps(payload) + "\n").encode())
            data = b""
            while not data.endswith(b"\n"):
                chunk = client.recv(65536)
                if not chunk:
                    break
                data += chunk
        return json.loads(data or b"{}")
    except (OSError, ValueError, KeyError):
        return None
