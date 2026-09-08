import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 单测禁止任何真实 TCP 连接；responses/FakeSIO 不受影响。
import socket
import pytest


@pytest.fixture(autouse=True)
def no_live_tcp(monkeypatch):
    original = socket.socket.connect
    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError(f"测试禁止真实 TCP 连接: {address}")
        return original(sock, address)
    monkeypatch.setattr(socket.socket, "connect", connect)
