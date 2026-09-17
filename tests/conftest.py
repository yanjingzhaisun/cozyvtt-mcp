import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Block all real TCP connections in tests; responses/FakeSIO are unaffected.
import socket
import pytest


@pytest.fixture(autouse=True)
def no_live_tcp(monkeypatch):
    original = socket.socket.connect
    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError(f"Tests prohibit real TCP connections: {address}")
        return original(sock, address)
    monkeypatch.setattr(socket.socket, "connect", connect)
