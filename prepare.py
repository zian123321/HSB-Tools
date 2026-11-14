#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prepare.py - 用 Python 读取 Excel，输出 chunks.json，避免 Node.js 加载大 Excel OOM
"""
import json, sys, os
from pathlib import Path

try:
    import openpyxl
except ImportError:
    print("Installing openpyxl...")
    os.system(f"{sys.executable} -m pip install openpyxl")
    import openpyxl

try:
    import xlrd
except ImportError:
    print("Installing xlrd...")
    os.system(f"{sys.executable} -m pip install xlrd")
    import xlrd

ROOT = Path(sys.argv[1]).resolve()
outDir = ROOT / '输出文件'
outDir.mkdir(parents=True, exist_ok=True)

# 读 config
config = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
FALLBACK = {'maxPerOrder': 500, 'barcodeType': '04', 'targetLocation': ''}
for k, v in FALLBACK.items():
    if config.get(k) is None or config.get(k) == '':
        config[k] = v

# ---- 幂等跳过：源清单与库存报表均未变化，且已有 chunks.json，则沿用旧切块 ----
def _stat_meta(p):
    if not p or not str(p).strip():
        return None
    pp = Path(p)
    if not pp.is_absolute():
        pp = ROOT / pp
    try:
        st = os.stat(pp)
        return {'path': str(pp), 'size': st.st_size, 'mtime': int(st.st_mtime)}
    except OSError:
        return None

source_path = config.get('sourceFile')
inventory_path = config.get('inventoryReport')
if not inventory_path or not str(inventory_path).strip():
    fb = outDir / '库存明细报表.xlsx'
    if fb.exists():
        inventory_path = str(fb)

chunks_path = outDir / 'chunks.json'
cur_meta = {'source': _stat_meta(source_path), 'inventory': _stat_meta(inventory_path)}

def _meta_saved_match(saved, cur):
    if not saved or not cur:
        return False
    return saved.get('path') == cur['path'] and saved.get('size') == cur['size'] and saved.get('mtime') == cur['mtime']

if chunks_path.exists():
    try:
        prev = json.loads(chunks_path.read_text(encoding='utf-8'))
        prev_meta = prev.get('meta') if isinstance(prev, dict) else None
        prev_chunks = prev.get('chunks') if isinstance(prev, dict) else prev
        if (prev_meta and prev_chunks and
                _meta_saved_match(prev_meta.get('source'), cur_meta['source']) and
                _meta_saved_match(prev_meta.get('inventory'), cur_meta['inventory'])):
            print(f"[跳过] 源清单与库存报表未变化，沿用已有切块：{len(prev_chunks)} 个批次")
            sys.exit(0)
    except Exception:
        pass

def clean(v):
    return '' if v is None else str(v).strip()

def read_excel(file_path, label):
    """读取 Excel，支持 .xlsx 和 .xls"""
    if not file_path or not str(file_path).strip():
        raise Exception(f"缺少{label}路径")
    abs_path = Path(file_path)
    if not abs_path.is_absolute():
        abs_path = ROOT / abs_path
    if not abs_path.exists():
        raise Exception(f"{label}文件不存在：{abs_path}")
    ext = abs_path.suffix.lower()
    print(f"  {label} 文件格式：{ext}，路径：{abs_path}")
    # 打印文件头几个字节，判断真实格式
    with open(abs_path, 'rb') as f:
        header = f.read(8)
    print(f"  {label} 文件头：{header.hex()}")
    if header[:4] == b'PK\x03\x04':
        print(f"  {label} 是标准 ZIP/XLSX 格式")
    elif header[:5] == b'<?xml' or header[:1] == b'<':
        print(f"  {label} 是 HTML/XML 格式（伪装成 xlsx）")
    elif header[:8] == b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1':
        print(f"  {label} 是老 XLS 格式（OLE2）")
    else:
        print(f"  {label} 未知格式")
    rows = []
    if ext == '.xls':
        # 用 xlrd 读 .xls
        wb = xlrd.open_workbook(abs_path)
        ws = wb.sheet_by_index(0)
        for r in range(ws.nrows):
            row = []
            for c in range(ws.ncols):
                v = ws.cell_value(r, c)
                row.append(clean(v))
            rows.append(row)
    else:
        # 用 openpyxl 读 .xlsx，不用 read_only 模式
        wb = openpyxl.load_workbook(abs_path, data_only=True)
        print(f"  {label} sheet 列表：{wb.sheetnames}")
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            sheet_rows = []
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= 30:  # 只读前30行找表头
                    break
                sheet_rows.append([clean(c) for c in row])
            # 如果这个 sheet 有超过 2 行数据，就用它
            data_rows = [r for r in sheet_rows if any(r)]
            if len(data_rows) >= 2:
                print(f"  {label} 使用 sheet：{sheet_name}，有效行数：{len(data_rows)}")
                print(f"    前3行：{sheet_rows[:3]}")
                # 读取完整数据
                rows = []
                for row in ws.iter_rows(values_only=True):
                    rows.append([clean(c) for c in row])
                break
        wb.close()
        if not rows:
            print(f"  {label} 警告：没找到多数据sheet，用第一个sheet")
    print(f"  {label}: 共 {len(rows)} 行")
    for i, r in enumerate(rows[:5]):
        print(f"    行{i}: {r[:8]}")
    return rows

def find_header(matrix, aliases, max_rows=20):
    for r in range(min(len(matrix), max_rows)):
        row = matrix[r]
        indexes = []
        found = True
        for group in aliases:
            idx = -1
            for i, v in enumerate(row):
                if v in group:
                    idx = i
                    break
            if idx < 0:
                found = False
                break
            indexes.append(idx)
        if found:
            return r, indexes
    # 找不到时打印前5行，方便调试
    print("错误：未找到表头。前5行内容：")
    for i, row in enumerate(matrix[:5]):
        print(f"  行{i}: {row[:8]}")
    raise Exception(f"未找到表头：{aliases}")

# 1. 读源商品清单
source = read_excel(config.get('sourceFile'), '源商品清单')
sh_row, sh_idx = find_header(source, [['商品条码', '条码'], ['类目', '类型', '品类']])
source_rows = []
for r in source[sh_row + 1:]:
    barcode = clean(r[sh_idx[0]]) if len(r) > sh_idx[0] else ''
    category = clean(r[sh_idx[1]]) if len(r) > sh_idx[1] else ''
    if barcode:
        source_rows.append({'barcode': barcode, 'category': category})
print(f"源商品清单：共 {len(source_rows)} 台")

# 2. 读库存报表
inventory_path = config.get('inventoryReport')
if not inventory_path or not str(inventory_path).strip():
    fallback = outDir / '库存明细报表.xlsx'
    if fallback.exists():
        inventory_path = str(fallback)
    else:
        raise Exception("未找到库存明细报表")
config['inventoryReport'] = inventory_path

inventory = read_excel(inventory_path, '库存报表')
ih_row, ih_idx = find_header(inventory, [['商品编码', '商品条码', '条码'], ['库位编码', '库位', '货位编码', '出库货位']])
inventory_header = inventory[ih_row]
item_code_index = inventory_header.index('商品编码') if '商品编码' in inventory_header else -1
barcode_index = inventory_header.index('商品条码') if '商品条码' in inventory_header else -1
location_index = inventory_header.index('库位编码') if '库位编码' in inventory_header else -1
available_index = inventory_header.index('可用数量') if '可用数量' in inventory_header else -1

if location_index < 0 or available_index < 0 or barcode_index < 0:
    raise Exception('库存报表缺少商品条码、库位编码或可用数量列')

# 3. 匹配条码和库位
exact_candidates = {}
exact_item_code_candidates = {}
for row in inventory[ih_row + 1:]:
    available = float(row[available_index]) if row[available_index] else 0
    location = clean(row[location_index])
    if not location:
        continue
    exact_barcode = clean(row[barcode_index]) if barcode_index >= 0 else ''
    item_code = clean(row[item_code_index]) if item_code_index >= 0 else ''
    if exact_barcode.endswith('_1') or item_code.endswith('_1'):
        continue
    if exact_barcode:
        exact_candidates.setdefault(exact_barcode, []).append({'location': location, 'available': available})
    if item_code:
        exact_item_code_candidates.setdefault(item_code, []).append({'location': location, 'available': available})

by_barcode = {}
for row in source_rows:
    barcode = row['barcode']
    if barcode.endswith('_1'):
        candidates = []
    else:
        candidates = exact_candidates.get(barcode) or exact_item_code_candidates.get(barcode) or []
    candidates.sort(key=lambda x: (-x['available'], x['location']))
    if candidates:
        by_barcode[barcode] = [candidates[0]['location']]

# 4. 分组
groups = {'手机': [], '平板': [], '其他': []}
for row in source_rows:
    group = '手机' if row['category'] == '手机' else ('平板' if row['category'] == '平板' else '其他')
    locations = by_barcode.get(row['barcode'], [])
    groups[group].append({
        'barcode': row['barcode'],
        'srcCat': row['category'],
        'location': ' / '.join(sorted(locations)),
        'matched': len(locations) > 0
    })

for values in groups.values():
    values.sort(key=lambda x: (not x['matched'], x['location'], x['barcode']))

# 5. 剔除已在目标库位的；同时留存被剔除(已在位)与未匹配明细，便于核对去向
target_loc = config['targetLocation']
at_target = []
unmatched = []
skipped_by_cat = {}
for cat in groups:
    before = len(groups[cat])
    kept = []
    for v in groups[cat]:
        is_at_target = (v['location'] or '').strip() and all(l.strip() == target_loc for l in (v['location'] or '').split('/'))
        if is_at_target:
            at_target.append(v)
        else:
            kept.append(v)
            if not v['matched']:
                unmatched.append(v)
    groups[cat] = kept
    skipped = before - len(groups[cat])
    skipped_by_cat[cat] = skipped
    if skipped > 0:
        print(f"[过滤已在目标库位] {cat}: 跳过 {skipped} 台")

# 5.1 去向对照统计：按原始类目列出 总数 / 已在目标库位剔除 / 未匹配 / 保留进移库清单
from collections import defaultdict
stat = defaultdict(lambda: {'total': 0, 'atTarget': 0, 'unmatched': 0, 'kept': 0})
for row in source_rows:
    stat[row['category']]['total'] += 1
for v in at_target:
    stat[v['srcCat']]['atTarget'] += 1
for v in unmatched:
    stat[v['srcCat']]['unmatched'] += 1
for cat in groups:
    for v in groups[cat]:
        stat[v['srcCat']]['kept'] += 1

print("\n===== 按类目条码匹配去向统计 =====")
total_src = len(source_rows)
total_skip_target = len(at_target)
total_unmatched = len(unmatched)
total_keep = sum(len(groups[c]) for c in groups)
for src_cat, s in sorted(stat.items(), key=lambda kv: -kv[1]['total']):
    print(f"  {src_cat}: 总 {s['total']} 台；已在目标库位剔除 {s['atTarget']} 台；未匹配 {s['unmatched']} 台；保留 {s['kept']} 台")
print(f"  合计：源清单 {total_src} 台；已在目标库位剔除 {total_skip_target} 台；未匹配 {total_unmatched} 台；最终进移库清单 {total_keep} 台")
if total_unmatched > 0:
    print(f"  提示：有 {total_unmatched} 台在库存报表中未匹配到库位，请核对条码是否与系统一致、库存报表是否导出完整。")
print("===========================\n")

# 5.2 落一份过滤明细，便于人工核对每条货的去向
detail_path = outDir / '过滤明细.json'
detail_path.write_text(json.dumps({
    'targetLocation': target_loc,
    'kept': [{'barcode': v['barcode'], 'category': v['srcCat'], 'location': v['location']} for g in groups.values() for v in g],
    'atTarget': [{'barcode': v['barcode'], 'category': v['srcCat'], 'location': v['location']} for v in at_target],
    'unmatched': [{'barcode': v['barcode'], 'category': v['srcCat'], 'location': v['location']} for v in unmatched],
}, ensure_ascii=False, indent=2), encoding='utf-8')
print(f"过滤明细已保存: {detail_path.name}（保留 {total_keep} / 已在位 {total_skip_target} / 未匹配 {total_unmatched}）")

# 6. 切块
chunks = []
max_per_order = config['maxPerOrder']
import_xls_dir = outDir / 'import_xls'
import_xls_dir.mkdir(exist_ok=True)

for category in ['手机', '平板', '其他']:
    values = groups[category]
    count = (len(values) + max_per_order - 1) // max_per_order
    for i in range(count):
        rows = values[i * max_per_order: (i + 1) * max_per_order]
        name = f"{category}_{i+1}" if count > 1 else category
        chunks.append({
            'name': name,
            'category': category,
            'rows': rows,
            'expected': len(rows),
            'matched': sum(1 for r in rows if r['matched']),
            'unmatched': sum(1 for r in rows if not r['matched']),
            'xlsPath': str(import_xls_dir / f"{name}_移库导入.xls")
        })
        print(f"[切块] {name}: 共 {len(rows)} 行")

# 7. 输出（带 meta，供下次重跑判断是否可沿用）
kept_rows = [r for g in groups.values() for r in g]
payload = {
    'meta': {'source': cur_meta['source'], 'inventory': cur_meta['inventory']},
    'chunks': chunks,
    'excluded': {
        'atTarget': [{'barcode': v['barcode'], 'category': v['srcCat'], 'location': v['location'], 'matched': v['matched']} for v in at_target],
        'unmatched': [{'barcode': v['barcode'], 'category': v['srcCat'], 'location': v['location'], 'matched': v['matched']} for v in unmatched],
    },
}
chunks_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
print(f"\n完成：共 {len(chunks)} 个切块，总 {sum(c['expected'] for c in chunks)} 行；已在位 {len(at_target)}；未匹配 {len(unmatched)}")
