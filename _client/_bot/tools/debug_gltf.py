import json, struct

with open('tools/nav_gltf.gltf', encoding='utf-8') as f:
    gltf = json.load(f)
bin_data = (Path := __import__('pathlib').Path)('tools/nav_gltf.bin').read_bytes()

# Читаем первые 12 индексов
bv = gltf['bufferViews'][3]
off = bv.get('byteOffset', 0)
indices = list(struct.unpack_from('12I', bin_data, off))
print('Первые 12 индексов:', indices)

# Читаем первые 6 позиций
bv0 = gltf['bufferViews'][0]
off0 = bv0.get('byteOffset', 0)
for i in range(6):
    x,y,z = struct.unpack_from('fff', bin_data, off0 + i*12)
    print(f'vertex[{i}]: x={x:.2f} y={y:.2f} z={z:.2f}')

# Проверяем - есть ли повторяющиеся индексы (shared vertices)
all_indices = list(struct.unpack_from(f'{gltf["accessors"][3]["count"]}I', bin_data, off))
unique = len(set(all_indices))
print(f'Всего индексов: {len(all_indices)}, уникальных вершин используется: {unique}')
print(f'Макс индекс: {max(all_indices)}, всего вершин: {gltf["accessors"][0]["count"]}')
