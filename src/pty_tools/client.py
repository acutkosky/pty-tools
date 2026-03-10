"""Synchronous client for communicating with PTY servers via Unix domain sockets."""

import json
import socket

from pty_tools.common import is_server_alive, socket_path_for


class PTYClientError(Exception):
    pass


def send_request(session_id: str, message: dict, timeout: float = 30.0) -> dict:
    """Send a request to a PTY server and receive its response."""
    sock_path = str(socket_path_for(session_id))

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)

    try:
        sock.connect(sock_path)
    except (ConnectionRefusedError, FileNotFoundError):
        if not is_server_alive(session_id):
            raise PTYClientError(
                f"Session '{session_id}' is not running. It may have exited. "
                f"Use pty_list to see active sessions."
            )
        raise PTYClientError(
            f"Cannot connect to session '{session_id}' — connection refused."
        )

    try:
        sock.sendall(json.dumps(message).encode())
        sock.shutdown(socket.SHUT_WR)

        response = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk

        return json.loads(response.decode())
    finally:
        sock.close()
