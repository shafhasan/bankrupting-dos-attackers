from __future__ import annotations

import csv
import json
import socket
import threading
import time
from pathlib import Path
from typing import Any, Iterable

BENIGN_LABEL = "BENIGN"
PROTOCOL_TCP = 6
PROTOCOL_UDP = 17


def strip_columns(columns: Iterable[Any]) -> list[str]:
    return [str(column).strip() for column in columns]


def json_dumps_line(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def recv_json_line(
    sock: socket.socket,
    buffer: bytearray | None = None,
) -> tuple[dict[str, Any], bytearray]:
    if buffer is None:
        buffer = bytearray()
    while True:
        newline = buffer.find(b"\n")
        if newline >= 0:
            raw = bytes(buffer[:newline])
            del buffer[: newline + 1]
            if not raw.strip():
                continue
            return json.loads(raw.decode("utf-8")), buffer
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError("Connection closed before a complete JSON line was received")
        buffer.extend(chunk)


class JsonLineClient:
    def __init__(self, host: str, port: int, timeout: float = 10.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.socket: socket.socket | None = None
        self.buffer = bytearray()

    def _connect(self) -> None:
        self.close()
        self.socket = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.socket.settimeout(self.timeout)
        self.buffer = bytearray()

    def request(self, message: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(2):
            try:
                if self.socket is None:
                    self._connect()
                assert self.socket is not None
                self.socket.sendall(json_dumps_line(message))
                response, self.buffer = recv_json_line(self.socket, self.buffer)
                return response
            except (OSError, ConnectionError):
                self._connect()
                if attempt == 1:
                    raise
        raise RuntimeError("unreachable")

    def close(self) -> None:
        if self.socket is not None:
            try:
                self.socket.close()
            finally:
                self.socket = None
                self.buffer = bytearray()


def request_json(
    host: str,
    port: int,
    message: dict[str, Any],
    timeout: float = 10.0,
) -> dict[str, Any]:
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(json_dumps_line(message))
        response, _ = recv_json_line(sock)
        return response


def wait_for_port(host: str, port: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError as exc:
            last_error = exc
            time.sleep(0.05)
    raise TimeoutError(f"Timed out waiting for {host}:{port}: {last_error}")


class CsvAppender:
    def __init__(self, path: Path, fieldnames: list[str]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fieldnames = fieldnames
        self.lock = threading.Lock()
        self.handle = self.path.open("w", newline="", encoding="utf-8")
        self.writer = csv.DictWriter(self.handle, fieldnames=fieldnames, extrasaction="ignore")
        self.writer.writeheader()
        self.handle.flush()

    def write(self, row: dict[str, Any]) -> None:
        with self.lock:
            self.writer.writerow({name: row.get(name, "") for name in self.fieldnames})

    def flush(self) -> None:
        with self.lock:
            if not self.handle.closed:
                self.handle.flush()

    def close(self) -> None:
        with self.lock:
            if not self.handle.closed:
                self.handle.flush()
                self.handle.close()


class TracePacer:
    def __init__(self, speedup: float):
        self.speedup = float(speedup)
        self.first_trace_time: float | None = None
        self.first_wall_time: float | None = None

    def wait(self, trace_time: float) -> None:
        if self.speedup <= 0:
            return
        if self.first_trace_time is None:
            self.first_trace_time = trace_time
            self.first_wall_time = time.monotonic()
            return
        assert self.first_wall_time is not None
        target = self.first_wall_time + (trace_time - self.first_trace_time) / self.speedup
        delay = target - time.monotonic()
        if delay > 0:
            time.sleep(delay)
