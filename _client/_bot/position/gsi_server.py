"""Сервер CS2 Game State Integration (GSI).

CS2 шлёт HTTP POST на указанный URI каждые ~100 мс, включая:
  player.position  — "x, y, z"
  player.forward   — "fx, fy, fz"  (единичный вектор взгляда)
  player.state     — здоровье, броня, ...
  map.name         — название карты

Для активации скопируй gamestate_integration.cfg в папку cfg CS2.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Dict, Optional, Tuple

_LOG = logging.getLogger(__name__)


class PlayerState:
    """Снимок состояния игрока, полученный через GSI."""

    __slots__ = (
        "x", "y", "z",
        "forward_x", "forward_y", "forward_z",
        "health", "team", "map_name", "valid",
    )

    def __init__(self) -> None:
        self.x: float = 0.0
        self.y: float = 0.0
        self.z: float = 0.0
        self.forward_x: float = 1.0
        self.forward_y: float = 0.0
        self.forward_z: float = 0.0
        self.health: int = 0
        self.team: str = ""
        self.map_name: str = ""
        self.valid: bool = False

    @property
    def position(self) -> Tuple[float, float, float]:
        return self.x, self.y, self.z

    @property
    def yaw_deg(self) -> float:
        """Текущий курс (градусы) из forward-вектора. 0° = восток (+X)."""
        return math.degrees(math.atan2(self.forward_y, self.forward_x))

    def copy(self) -> "PlayerState":
        s = PlayerState()
        for attr in self.__slots__:
            setattr(s, attr, getattr(self, attr))
        return s


def _parse_vec3(raw: str) -> Optional[Tuple[float, float, float]]:
    """Разбирает строку "x, y, z" → (float, float, float)."""
    try:
        parts = [float(p.strip()) for p in raw.split(",")]
        if len(parts) >= 3:
            return parts[0], parts[1], parts[2]
    except ValueError:
        pass
    return None


class GsiServer:
    """Принимает GSI-обновления от CS2 на localhost."""

    def __init__(self, host: str = "127.0.0.1", port: int = 3000) -> None:
        self._host = host
        self._port = port
        self._state = PlayerState()
        self._lock = threading.Lock()
        self._on_update: Optional[Callable[[PlayerState], None]] = None
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.packets_received: int = 0
        self.last_packet_time: float = 0.0

    # ------------------------------------------------------------------
    @property
    def state(self) -> PlayerState:
        """Возвращает копию текущего состояния (потокобезопасно)."""
        with self._lock:
            return self._state.copy()

    def on_update(self, callback: Callable[[PlayerState], None]) -> None:
        """Регистрирует callback, вызываемый при каждом GSI-обновлении."""
        self._on_update = callback

    # ------------------------------------------------------------------
    def _parse_payload(self, data: Dict[str, Any]) -> None:
        self.packets_received += 1
        self.last_packet_time = time.monotonic()

        player: Dict[str, Any] = data.get("player", {})
        map_data: Dict[str, Any] = data.get("map", {})

        with self._lock:
            pos = _parse_vec3(player.get("position", ""))
            if pos:
                self._state.x, self._state.y, self._state.z = pos
                self._state.valid = True

            fwd = _parse_vec3(player.get("forward", ""))
            if fwd:
                self._state.forward_x, self._state.forward_y, self._state.forward_z = fwd

            pstate: Dict[str, Any] = player.get("state", {})
            if "health" in pstate:
                self._state.health = int(pstate["health"])

            if "team" in player:
                self._state.team = player["team"]

            if "name" in map_data:
                self._state.map_name = map_data["name"]

            snapshot = self._state.copy()

        cb = self._on_update
        if cb is not None:
            try:
                cb(snapshot)
            except Exception:
                _LOG.exception("Ошибка в GSI on_update callback")

    # ------------------------------------------------------------------
    def start(self) -> None:
        gsi = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                self.send_response(200)
                self.end_headers()
                try:
                    gsi._parse_payload(json.loads(body))
                except Exception as exc:
                    _LOG.debug("GSI parse error: %s", exc)

            def log_message(self, fmt: str, *args: Any) -> None:  # noqa: N802
                pass  # Подавляем HTTP-логи

        self._server = HTTPServer((self._host, self._port), _Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="gsi-server",
        )
        self._thread.start()
        _LOG.info("GSI сервер запущен: http://%s:%d", self._host, self._port)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            _LOG.info("GSI сервер остановлен")
