"""
HTTP-приёмник позиций от бота -> накопление waypoints в JSON.

Запуск из корня демо:
    python -m waypoints.record_server --out waypoints/maps/bot_de_dust2.json --map de_dust2 --port 9777

Бот (любой язык) шлёт:
    POST http://127.0.0.1:9777/add
    Content-Type: application/json
    {"x": 123.4, "y": -56.7, "z": 2.0, "tags": ["mid"], "dedup": true, "link": true}

GET /health -> {"ok": true}
POST /save  -> сбросить текущий граф на диск (тот же путь, что --out)
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Optional

from .recorder import WaypointRecorder

_LOG = logging.getLogger("waypoints.record_server")

_state: dict[str, Any] = {}


class _Handler(BaseHTTPRequestHandler):
    server_version = "WaypointRecorder/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        _LOG.info("%s - " + fmt, self.address_string(), *args)

    def _json_response(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Optional[dict]:
        length = self.headers.get("Content-Length")
        if length is None:
            return None
        try:
            n = int(length)
        except ValueError:
            return None
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return None

    def do_GET(self) -> None:
        if self.path.startswith("/health"):
            self._json_response(200, {"ok": True})
        else:
            self._json_response(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if self.path.startswith("/add"):
            data = self._read_json()
            if data is None:
                self._json_response(400, {"error": "invalid_json"})
                return
            try:
                x = float(data["x"])
                y = float(data["y"])
                z = float(data.get("z", 0.0))
            except (KeyError, TypeError, ValueError):
                self._json_response(400, {"error": "need x,y number and optional z"})
                return

            rec: WaypointRecorder = _state["recorder"]
            raw_tags = data.get("tags")
            tags = list(raw_tags) if isinstance(raw_tags, list) else []
            dedup = bool(data.get("dedup", True))
            link_previous = bool(data.get("link", True))

            nid = rec.add_point(x, y, z, tags=tags, dedup=dedup, link_previous=link_previous)

            out_path = _state.get("out_path")
            saved = False
            if out_path is not None:
                try:
                    rec.save_json(out_path)
                    saved = True
                except Exception:
                    pass

            self._json_response(
                200,
                {
                    "ok": True,
                    "node_id": str(nid) if nid is not None else None,
                    "total_nodes": len(rec._nodes),
                    "saved": saved,
                },
            )

        elif self.path.startswith("/save"):
            rec = _state.get("recorder")
            out_path = _state.get("out_path")
            if rec is None or out_path is None:
                self._json_response(404, {"error": "not_found"})
                return
            rec.save_json(out_path)
            self._json_response(200, {"ok": True, "nodes": len(rec._nodes), "path": str(out_path)})

        else:
            self._json_response(404, {"error": "not_found"})


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    p = argparse.ArgumentParser(
        description="HTTP waypoint recorder for bot position samples"
    )
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9777)
    p.add_argument("--out", required=True, help="Output JSON path")
    p.add_argument("--map", default="recorded", help="map name field in JSON")
    p.add_argument("--dedup", type=float, default=32.0, help="Min distance between nodes")
    p.add_argument("--resume", action="store_true", help="Append to existing JSON if present")
    args = p.parse_args()

    Path = pathlib.Path
    out = Path(args.out)

    if args.resume and out.is_file():
        rec = WaypointRecorder.load_merge_points(out, map_name=args.map)
        rec.dedup_radius = args.dedup
        _LOG.info("Продолжение записи, узлов: %d", len(rec._nodes))
    else:
        rec = WaypointRecorder(map_name=args.map, dedup_radius=args.dedup)

    _state["recorder"] = rec
    _state["out_path"] = out.resolve()

    _LOG.info(
        "Запись waypoints: POST http://%s:%s/add  JSON x,y,z -> %s",
        args.host,
        args.port,
        _state["out_path"],
    )

    httpd = HTTPServer((args.host, args.port), _Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        _LOG.info("Остановка, сохранение...")
        rec.save_json(_state["out_path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
