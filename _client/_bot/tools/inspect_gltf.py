import json, struct
with open('tools/nav_gltf.gltf', encoding='utf-8') as f:
    g = json.load(f)
prim = g['meshes'][0]['primitives'][0]
print('attributes:', list(prim.get('attributes', {}).keys()))
print('mode:', prim.get('mode', 4))
print('indices:', prim.get('indices', 'none'))
for i, acc in enumerate(g['accessors']):
    print('acc', i, acc)
for i, bv in enumerate(g.get('bufferViews', [])):
    print('bv', i, bv)
