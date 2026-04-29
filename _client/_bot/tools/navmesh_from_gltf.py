"""
Конвертер nav_gltf.gltf (экспорт из Source2Viewer-CLI) → CS2-AI navmesh JSON.

Пайплайн:
  1. Source2Viewer-CLI -i navmesh/de_inferno.nav -o tools --gltf_export_format gltf -d
  2. python tools/navmesh_from_gltf.py --gltf tools/nav_gltf.gltf --map de_inferno

Зависимости: только стандартная библиотека Python.
"""
from __future__ import annotations

import argparse
import json
import math
import struct
from pathlib import Path

_NAVMESH_DIR = Path(__file__).resolve().parent.parent / "navmesh"


# ---------------------------------------------------------------------------
# Чтение glTF (binary buffer .bin)
# ---------------------------------------------------------------------------

def _read_accessor_vec3(gltf: dict, bin_data: bytes, accessor_idx: int) -> list[tuple[float, float, float]]:
    acc = gltf["accessors"][accessor_idx]
    bv = gltf["bufferViews"][acc["bufferView"]]
    byte_offset = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    count = acc["count"]
    stride = bv.get("byteStride", 12)
    result = []
    for i in range(count):
        x, y, z = struct.unpack_from("fff", bin_data, byte_offset + i * stride)
        result.append((x, y, z))
    return result


def _read_accessor_u32(gltf: dict, bin_data: bytes, accessor_idx: int) -> list[int]:
    acc = gltf["accessors"][accessor_idx]
    bv = gltf["bufferViews"][acc["bufferView"]]
    byte_offset = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    count = acc["count"]
    component_type = acc["componentType"]
    fmt = {5121: "B", 5123: "H", 5125: "I"}[component_type]
    size = {"B": 1, "H": 2, "I": 4}[fmt]
    stride = bv.get("byteStride", size)
    return [struct.unpack_from(fmt, bin_data, byte_offset + i * stride)[0] for i in range(count)]


# ---------------------------------------------------------------------------
# Конвертация
# ---------------------------------------------------------------------------

def _dist3(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def _centroid3(pts: list[tuple[float, float, float]]) -> tuple[float, float, float]:
    n = len(pts)
    return (sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n, sum(p[2] for p in pts) / n)


def _greedy_downsample(points: list[tuple[float, float, float]], min_dist: float) -> list[int]:
    kept: list[int] = []
    kept_pos: list[tuple[float, float, float]] = []
    for i, pos in enumerate(points):
        if all(_dist3(pos, p) >= min_dist for p in kept_pos):
            kept.append(i)
            kept_pos.append(pos)
    return kept


def convert(gltf_path: Path, map_name: str, min_dist: float, out_path: Path) -> None:
    print(f"Читаю {gltf_path} ...")
    with open(gltf_path, encoding="utf-8") as f:
        gltf = json.load(f)

    # Путь к .bin файлу рядом с .gltf
    bin_uri = gltf["buffers"][0]["uri"]
    bin_path = gltf_path.parent / bin_uri
    bin_data = bin_path.read_bytes()

    # Находим primitive hull_0
    mesh = gltf["meshes"][0]
    prim = mesh["primitives"][0]

    pos_acc_idx = prim["attributes"]["POSITION"]
    idx_acc_idx = prim.get("indices")

    vertices = _read_accessor_vec3(gltf, bin_data, pos_acc_idx)
    print(f"Вершин: {len(vertices)}")

    # Индексы (треугольники, mode=4)
    if idx_acc_idx is not None:
        indices = _read_accessor_u32(gltf, bin_data, idx_acc_idx)
    else:
        indices = list(range(len(vertices)))

    # Каждые 3 индекса = треугольник
    triangles = [(indices[i], indices[i + 1], indices[i + 2]) for i in range(0, len(indices) - 2, 3)]
    print(f"Треугольников: {len(triangles)}")

    # Площадь треугольника (для сортировки — большие треугольники в центре зон, маленькие у стен)
    def _tri_area(a: int, b: int, c: int) -> float:
        va, vb, vc = vertices[a], vertices[b], vertices[c]
        ax, ay = vb[0] - va[0], vb[1] - va[1]
        bx, by = vc[0] - va[0], vc[1] - va[1]
        return abs(ax * by - ay * bx) * 0.5

    tri_areas = [_tri_area(a, b, c) for a, b, c in triangles]

    # Центроид каждого треугольника
    tri_centroids = [
        _centroid3([vertices[a], vertices[b], vertices[c]])
        for a, b, c in triangles
    ]

    # Строим граф смежности треугольников по общим рёбрам.
    # Вершины не разделяются по id (каждая уникальна), сравниваем по округлённым координатам.
    def _pos_key(vi: int) -> tuple[int, int, int]:
        p = vertices[vi]
        return (round(p[0] * 4), round(p[1] * 4), round(p[2] * 4))

    edge_to_tri: dict[frozenset, list[int]] = {}
    for ti, (a, b, c) in enumerate(triangles):
        for edge in (
            frozenset((_pos_key(a), _pos_key(b))),
            frozenset((_pos_key(b), _pos_key(c))),
            frozenset((_pos_key(a), _pos_key(c))),
        ):
            edge_to_tri.setdefault(edge, []).append(ti)

    tri_neighbors: list[set[int]] = [set() for _ in range(len(triangles))]
    for neighbors in edge_to_tri.values():
        if len(neighbors) == 2:
            a, b = neighbors
            tri_neighbors[a].add(b)
            tri_neighbors[b].add(a)

    # Сортируем по площади убыванию: крупные треугольники = центр зоны, вдали от стен.
    # Жадное прореживание теперь предпочитает их — узлы оказываются дальше от стен.
    sorted_tri_ids = sorted(range(len(triangles)), key=lambda i: tri_areas[i], reverse=True)
    sorted_centroids = [tri_centroids[i] for i in sorted_tri_ids]

    print(f"Прореживание (min_dist={min_dist:.0f}, сортировка по площади) ...")
    kept_sorted_indices = _greedy_downsample(sorted_centroids, min_dist)
    kept_tri_ids = [sorted_tri_ids[i] for i in kept_sorted_indices]
    print(f"Узлов после прореживания: {len(kept_tri_ids)}")

    # Маппинг: tri_id → node_id
    tri_to_node: dict[int, int] = {tid: ni for ni, tid in enumerate(kept_tri_ids)}
    kept_set = set(kept_tri_ids)

    # Для каждого треугольника найти ближайший kept-узел
    nearest: list[int] = [0] * len(triangles)
    for ti in range(len(triangles)):
        if ti in kept_set:
            nearest[ti] = tri_to_node[ti]
        else:
            pos = tri_centroids[ti]
            best_ni = min(kept_tri_ids, key=lambda k: _dist3(pos, tri_centroids[k]))
            nearest[ti] = tri_to_node[best_ni]

    # Узлы
    nodes = []
    for ni, ti in enumerate(kept_tri_ids):
        cx, cy, cz = tri_centroids[ti]
        # Угловой = мало соседей у треугольника
        is_corner = len(tri_neighbors[ti]) <= 1
        nodes.append({
            "id": ni,
            "x": round(cx, 4),
            "y": round(cy, 4),
            "z": round(cz, 4),
            "corner": is_corner,
        })

    # Рёбра: по смежности треугольников через маппинг
    edge_pairs: set[tuple[int, int]] = set()
    for ti in range(len(triangles)):
        src = nearest[ti]
        for nbr_ti in tri_neighbors[ti]:
            dst = nearest[nbr_ti]
            if src != dst:
                edge_pairs.add((src, dst))

    # Нормализуем пары → уникальные неупорядоченные рёбра, затем добавляем оба направления
    undirected: set[tuple[int, int]] = {
        (min(s, d), max(s, d)) for s, d in edge_pairs
    }
    edges = []
    for a, b in sorted(undirected):
        w = round(_dist3(
            (nodes[a]["x"], nodes[a]["y"], nodes[a]["z"]),
            (nodes[b]["x"], nodes[b]["y"], nodes[b]["z"]),
        ), 4)
        edges.append({"from": a, "to": b, "weight": w})
        edges.append({"from": b, "to": a, "weight": w})

    # Подключаем изолированные узлы к ближайшему соседу (устранение разрывов графа)
    from collections import defaultdict, deque

    def _find_component(start: int, adj: dict) -> set[int]:
        visited: set[int] = set()
        q: deque[int] = deque([start])
        visited.add(start)
        while q:
            n = q.popleft()
            for nb in adj.get(n, []):
                if nb not in visited:
                    visited.add(nb)
                    q.append(nb)
        return visited

    adj: dict[int, list[int]] = defaultdict(list)
    for e in edges:
        adj[e["from"]].append(e["to"])

    visited_all = _find_component(0, adj)
    isolated = [ni for ni in range(len(nodes)) if ni not in visited_all]

    if isolated:
        print(f"Подключаю {len(isolated)} изолированных узлов ...")
        extra_undirected: set[tuple[int, int]] = set()
        for iso in isolated:
            pos_iso = (nodes[iso]["x"], nodes[iso]["y"], nodes[iso]["z"])
            nearest_in_main = min(
                visited_all,
                key=lambda ni: _dist3(pos_iso, (nodes[ni]["x"], nodes[ni]["y"], nodes[ni]["z"])),
            )
            extra_undirected.add((min(iso, nearest_in_main), max(iso, nearest_in_main)))
            visited_all.add(iso)

        for a, b in sorted(extra_undirected):
            w = round(_dist3(
                (nodes[a]["x"], nodes[a]["y"], nodes[a]["z"]),
                (nodes[b]["x"], nodes[b]["y"], nodes[b]["z"]),
            ), 4)
            edges.append({"from": a, "to": b, "weight": w})
            edges.append({"from": b, "to": a, "weight": w})

    # Дедупликация рёбер
    seen_pairs: set[tuple[int, int]] = set()
    deduped_edges = []
    for e in edges:
        key = (e["from"], e["to"])
        if key not in seen_pairs:
            deduped_edges.append(e)
            seen_pairs.add(key)
    edges = deduped_edges

    result = {"map_name": map_name, "nodes": nodes, "edges": edges}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, separators=(",", ":")), encoding="utf-8")
    print(f"Сохранено: {out_path}")
    print(f"Узлов: {len(nodes)}, направленных рёбер: {len(edges)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Конвертер nav glTF → CS2-AI navmesh JSON")
    parser.add_argument("--gltf", required=True, help="Путь к .gltf файлу")
    parser.add_argument("--map", required=True, dest="map_name", help="Имя карты")
    parser.add_argument("--min-dist", type=float, default=150.0, help="Мин. расстояние между узлами (default: 150)")
    parser.add_argument("--out", default=None, help="Путь к выходному JSON")
    args = parser.parse_args()

    gltf_path = Path(args.gltf)
    if not gltf_path.exists():
        raise SystemExit(f"Файл не найден: {gltf_path}")
    out_path = Path(args.out) if args.out else (_NAVMESH_DIR / f"{args.map_name}.json")
    convert(gltf_path, args.map_name, args.min_dist, out_path)


if __name__ == "__main__":
    main()
