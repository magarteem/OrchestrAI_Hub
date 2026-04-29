# Создание navmesh для новой карты CS2

Пайплайн из трёх шагов — без внешних зависимостей Python.

---

## Что нужно

| Инструмент | Где взять |
|---|---|
| `Source2Viewer-CLI.exe` | Уже лежит в `tools/S2VCLI/` (скачан автоматически) |
| `de_<map>.nav` | Извлечь из VPK карты (инструкция ниже) |
| Python 3.9+ | Уже установлен |

---

## Шаг 1 — Получить `.nav` файл из VPK

### Вариант A: через Source 2 Viewer GUI
1. Скачать [Source 2 Viewer](https://s2v.app) (бесплатный, portable)
2. `File → Open` → выбрать `steamapps\common\Counter-Strike Global Offensive\game\csgo\maps\de_<map>.vpk`
3. В дереве файлов найти `maps/de_<map>.nav`
4. ПКМ → **Export as is** → сохранить в папку `navmesh/`

### Вариант B: через CLI (команда)
```powershell
tools\S2VCLI\Source2Viewer-CLI.exe `
  -i "C:\Program Files (x86)\Steam\steamapps\common\Counter-Strike Global Offensive\game\csgo\maps\de_mirage.vpk" `
  -o navmesh `
  -f maps/de_mirage.nav
```

> **Важно:** у CS2 файлы `.nav` упакованы внутри `.vpk`. Просто зайти в папку `maps/` и взять файл не получится.

---

## Шаг 2 — Экспортировать `.nav` в glTF

```powershell
tools\S2VCLI\Source2Viewer-CLI.exe `
  -i navmesh\de_mirage.nav `
  -o tools `
  --gltf_export_format gltf `
  -d
```

Будут созданы два файла:
- `tools\nav_gltf.gltf` — описание структуры (JSON)
- `tools\nav_gltf.bin` — бинарные данные вершин

Переименуйте их чтобы не перезаписать при следующей карте:
```powershell
Rename-Item tools\nav_gltf.gltf tools\de_mirage.gltf
Rename-Item tools\nav_gltf.bin  tools\de_mirage.bin
```

---

## Шаг 3 — Конвертировать glTF → CS2-AI navmesh JSON

```powershell
python tools\navmesh_from_gltf.py `
  --gltf tools\de_mirage.gltf `
  --map de_mirage
```

Результат сохраняется в `navmesh/de_mirage.json` и автоматически подхватывается ботом при запуске с `--map de_mirage`.

### Параметры конвертера

| Параметр | По умолчанию | Описание |
|---|---|---|
| `--gltf` | — | Путь к `.gltf` файлу (обязательный) |
| `--map` | — | Имя карты, используется в `map_name` JSON (обязательный) |
| `--min-dist` | `150` | Минимальное расстояние между узлами (units). Меньше → больше узлов, точнее. Больше → меньше узлов, быстрее. |
| `--out` | `navmesh/<map>.json` | Путь к выходному файлу |

**Подобрать `--min-dist`:**
- `100` — ~400–500 узлов, подходит для больших карт (de_dust2, de_nuke)
- `150` — ~200–300 узлов (баланс)
- `200` — ~100–150 узлов, как в оригинальных файлах проекта

---

## Быстрый пример для de_nuke

```powershell
# Шаг 1: .nav уже извлечён через S2V GUI → navmesh/de_nuke.nav

# Шаг 2: экспорт в glTF
tools\S2VCLI\Source2Viewer-CLI.exe -i navmesh\de_nuke.nav -o tools --gltf_export_format gltf -d
Rename-Item tools\nav_gltf.gltf tools\de_nuke.gltf
Rename-Item tools\nav_gltf.bin  tools\de_nuke.bin

# Шаг 3: конвертация
python tools\navmesh_from_gltf.py --gltf tools\de_nuke.gltf --map de_nuke --min-dist 150
```

Запуск бота:
```powershell
python nav_demo.py --map de_nuke
```

---

## Что внутри navmesh JSON (формат CS2-AI)

```json
{
  "map_name": "de_mirage",
  "nodes": [
    {"id": 0, "x": -100.0, "y": 200.0, "z": 5.0, "corner": false},
    ...
  ],
  "edges": [
    {"from": 0, "to": 1, "weight": 175.3},
    {"from": 1, "to": 0, "weight": 175.3},
    ...
  ]
}
```

- `nodes` — waypoints на карте (центроиды nav-областей), `corner=true` означает тупик или узкий проход
- `edges` — двунаправленные рёбра с весом = евклидово расстояние между узлами
- A* использует `weight` как стоимость перехода

---

## Look-точки для направлений A->B

Теперь в `navmesh/<map>.json` поддерживается секция `look_edges` для управления взглядом на направленных переходах.

Пример:

```json
{
  "look_edges": {
    "1->2": { "mode": "look_point", "x": 420.0, "y": -180.0 },
    "2->1": { "mode": "fixed_yaw", "yaw": 135.0 }
  }
}
```

- `1->2` и `2->1` настраиваются независимо.
- `mode=look_point` — смотреть в мировую точку.
- `mode=fixed_yaw` — держать фиксированный угол.

### Как редактировать в NavMesh Editor

1. Запусти редактор:
   - `python tools/navmesh_editor.py --map de_poseidon --mode wingman`
2. Нажми `L` (режим LOOK).
3. ЛКМ по узлу `A`, затем ЛКМ по узлу `B` (получится направленное ребро `A->B`).
4. ЛКМ в пустом месте — назначить `look_point`.
5. Нажми `A` — назначить `fixed_yaw` по направлению на курсор.
6. Нажми `K` — удалить настройку `A->B`.
7. Нажми `S` для сохранения.

### Проверка в nav_demo

- `python nav_demo.py --map de_poseidon --control-mode move+look`
- `python nav_demo.py --map de_poseidon --control-mode look_only`

`move+look` — бот двигается и смотрит по `look_edges`.  
`look_only` — бот управляет только мышью, движение остается за игроком.
