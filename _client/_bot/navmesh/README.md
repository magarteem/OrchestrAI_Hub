# Navmesh — доступные карты и команды запуска

Все команды выполняются из папки `cs2_player_detection_demo`.

## Доступные карты

| Карта         | Узлов | Рёбер | Режим        |
|---------------|-------|-------|--------------|
| de_dust2      | 108   | 236   | Competitive  |
| de_inferno    | 251   | 710   | Competitive  |
| de_nuke       | 345   | 994   | Competitive  |
| de_overpass   | 306   | 948   | Competitive  |
| de_poseidon   | 193   | 548   | Wingman 2v2  |
| de_sanctum    | 128   | 374   | Wingman 2v2  |
| workshop_460183010_1v1 | — | —  | 1v1 Workshop |

---

## Быстрый старт

```bash
# Чтение позиции из памяти CS2 (рекомендуется)
python nav_demo.py --map de_dust2 --reader memory
python nav_demo.py --map de_inferno --reader memory
python nav_demo.py --map de_nuke --reader memory
python nav_demo.py --map de_overpass --reader memory
python nav_demo.py --map de_poseidon --reader memory
python nav_demo.py --map de_sanctum --reader memory
python nav_demo.py --map workshop_460183010_1v1 --reader memory
```

После запуска нажми **Ctrl+N** — бот выберет случайную цель и начнёт двигаться.

---

## Все параметры nav_demo.py

| Параметр | По умолчанию | Описание |
|---|---|---|
| `--map <name>` | `de_dust2` | Название карты (должен быть `navmesh/<name>.json`) |
| `--reader memory` | — | Читать позицию из памяти `cs2.exe` (требует `pymem`) |
| `--reader radar` | radar | Читать позицию через YOLO-детекцию на радаре |
| `--target <id>` | нет | ID узла navmesh как цель при старте |
| `--record` | выкл | Записывать пройденный путь в JSON |
| `--record-out <path>` | `waypoints/maps/bot_<map>.json` | Куда сохранять запись пути |
| `--record-dedup <float>` | `32.0` | Мин. расстояние между записываемыми точками |

---

## Горячие клавиши

| Клавиша | Действие |
|---|---|
| **Ctrl+N** | Выбрать случайный узел как новую цель |
| **P** | Пауза / возобновление движения |
| **R** | Включить / выключить запись waypoints (если `--record`) |
| **Q** | Выход |

---

## Примеры с параметрами

```bash
# Стартовать сразу к узлу 42
python nav_demo.py --map de_inferno --reader memory --target 42

# Записать маршрут бота в файл
python nav_demo.py --map de_nuke --reader memory --record

# Записать маршрут в свой файл
python nav_demo.py --map de_nuke --reader memory --record --record-out my_route.json

# Режим радара (не нужен pymem, нужна обученная YOLO-модель)
python nav_demo.py --map de_dust2 --reader radar
```

---

## Режим радара (--reader radar)

Альтернативный способ читать позицию игрока — не из памяти CS2, а по скриншоту
радара в реальном времени. Не требует `pymem` и прямого доступа к памяти процесса.

### Как это работает

```
Экран CS2 → MSS (скриншот радара) → YOLO-модель → пиксельные координаты
    → аффинное преобразование (калибровка) → мировые координаты X, Y
```

### Требования

```bash
pip install ultralytics torch mss pywin32 opencv-python
```

### Шаг 1 — Настройки CS2

Задай в консоли CS2 (один раз, можно в `autoexec.cfg`):

```
cl_radar_rotate 0
cl_radar_always_centered 0
cl_hud_radar_scale 1.0
cl_radar_scale 0.7
```

### Шаг 2 — Калибровка радара

Запусти в тренировочном матче на нужной карте. Бот подключится к памяти,
будет смотреть на радар и автоматически привяжет пиксели к мировым координатам.
**Нужно походить по карте ~2–3 минуты**, пока не накопится достаточно точек.

```bash
python -m radar_position.calibration.calibrator --map de_dust2
python -m radar_position.calibration.calibrator --map de_inferno
# и т.д. для каждой карты
```

Результат сохраняется в `radar_position/calibration/<map>.json`.

### Шаг 3 — Сбор датасета

Датасет — скриншоты радара с разметкой положения точки игрока.
Записывается автоматически во время игры:

```bash
python -m dataset_tools.collector --map de_dust2
```

Изображения сохраняются в `dataset_tools/dataset/de_dust2/`.

### Шаг 4 — Обучение YOLO-модели

```bash
python -m dataset_tools.trainer --map de_dust2
# или с параметрами:
python -m dataset_tools.trainer --map de_dust2 --model yolo12n --epochs 50
```

Обученная модель сохраняется в `radar_position/weights/de_dust2_best.pt`.

### Шаг 5 — Запуск бота в режиме радара

```bash
python nav_demo.py --map de_dust2 --reader radar
python nav_demo.py --map de_inferno --reader radar
```

### Сравнение режимов

| | `--reader memory` | `--reader radar` |
|---|---|---|
| Доступ к памяти CS2 | нужен (`pymem`) | не нужен |
| Точность позиции | высокая | средняя |
| Зависит от патчей | да (офсеты) | нет |
| Требует обучения | нет | да (YOLO + калибровка) |
| Работает без экрана | да | нет |

---

## Обновление офсетов памяти

Офсеты меняются при каждом обновлении CS2. После обновления игры выполни:

```bash
python fetch_offsets.py
```

---

## Добавить новую карту

Полная инструкция: `tools/README_navmesh.md`

Краткий алгоритм:
1. Достань `<map>.nav` из VPK CS2 через [Source 2 Viewer](https://valveresourceformat.github.io/)
2. Экспортируй в glTF:
   ```bash
   tools\S2VCLI\Source2Viewer-CLI.exe -i navmesh\<map>.nav -o tools\<map>_out --gltf_export_format gltf -d
   ```
3. Сгенерируй JSON:
   ```bash
   python tools/navmesh_from_gltf.py --gltf tools/<map>_out.gltf --map <map> --min-dist 150
   ```
