#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pure Python XLS generator (no LibreOffice dependency).
Uses xlwt to write BIFF .xls directly - stable, no external process, no empty files.
"""
import json
import os
import sys
import subprocess

# Ensure xlwt is installed
try:
    import xlwt
except ImportError:
    print('Installing xlwt...')
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'xlwt', '-q'])
    import xlwt


def main():
    root = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else '.')
    chunks_path = os.path.join(root, '输出文件', 'chunks.json')
    output_dir = os.path.join(root, '输出文件', 'import_xls')
    os.makedirs(output_dir, exist_ok=True)

    # Read config
    config_path = os.path.join(root, 'config.json')
    cfg = {}
    if os.path.isfile(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
    barcode_type = str(cfg.get('barcodeType') or '04')
    target_location = str(cfg.get('targetLocation') or '')
    print(f'Barcode type={barcode_type}, target location={target_location}')

    with open(chunks_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    chunks = data['chunks'] if isinstance(data, dict) else data

    def _xls_row_count_matches(path, expected):
        # 校验已有 xls：表头 1 行 + 数据行，数据行数与切块一致则视为有效
        try:
            import xlrd
            book = xlrd.open_workbook(path)
            sh = book.sheet_by_index(0)
            return sh.nrows - 1 == expected
        except Exception:
            return False

    for chunk in chunks:
        rows = chunk['rows']
        name = chunk['name']
        target = os.path.join(output_dir, f'{name}_移库导入.xls')
        # 幂等跳过：已有 xls 非空且数据行数与 expected 一致，不再重复生成；缺失/损坏则补生成
        if os.path.exists(target) and os.path.getsize(target) > 0 and _xls_row_count_matches(target, len(rows)):
            print(f'[跳过] {target} 已存在且行数匹配（{len(rows)} 行）')
            continue
        wb = xlwt.Workbook(encoding='utf-8')
        ws = wb.add_sheet('Sheet1')
        # Header row (same as WMS template)
        headers = ['出库库位', '商品条码', '数量', '条码类型', '入库库位']
        for col, h in enumerate(headers):
            ws.write(0, col, h)
        # Data rows
        for i, row in enumerate(rows, start=1):
            ws.write(i, 0, str(row.get('location', '')))
            ws.write(i, 1, str(row.get('barcode', '')))
            ws.write(i, 2, 1)
            ws.write(i, 3, barcode_type)
            ws.write(i, 4, target_location)
        target = os.path.join(output_dir, f'{name}_移库导入.xls')
        wb.save(target)
        size = os.path.getsize(target)
        if size == 0:
            raise RuntimeError(f'Output file is 0 bytes: {target}')
        print(f'Created: {target} ({len(rows)} rows, {size} bytes)')

    print('XLS conversion complete (pure Python, no LibreOffice).')

    # 全量对照表：保留移库 / 未匹配 / 已在目标库位，三类互斥，覆盖源清单全部货品
    excluded = data.get('excluded', {}) if isinstance(data, dict) else {}
    at_target = excluded.get('atTarget', []) if isinstance(excluded, dict) else []

    full_rows = []
    for chunk in chunks:
        for r in chunk['rows']:
            status = '保留移库' if r.get('matched') else '未匹配'
            full_rows.append({
                'category': r.get('srcCat') or r.get('category') or chunk.get('category', ''),
                'status': status,
                'location': r.get('location', ''),
                'barcode': r.get('barcode', ''),
            })
    for r in at_target:
        full_rows.append({
            'category': r.get('category', ''),
            'status': '已在目标库位',
            'location': r.get('location', ''),
            'barcode': r.get('barcode', ''),
        })

    full_target = os.path.join(output_dir, '全部货品对照表.xls')
    need_full = True
    if os.path.exists(full_target) and os.path.getsize(full_target) > 0:
        try:
            import xlrd
            _b = xlrd.open_workbook(full_target)
            _sh = _b.sheet_by_index(0)
            need_full = (_sh.nrows - 1) != len(full_rows)
        except Exception:
            need_full = True
    if need_full:
        wb = xlwt.Workbook(encoding='utf-8')
        ws = wb.add_sheet('Sheet1')
        headers = ['类目', '去向', '出库库位', '商品条码', '数量', '条码类型', '入库库位']
        for col, h in enumerate(headers):
            ws.write(0, col, h)
        for i, r in enumerate(full_rows, start=1):
            ws.write(i, 0, str(r['category']))
            ws.write(i, 1, str(r['status']))
            ws.write(i, 2, str(r['location']))
            ws.write(i, 3, str(r['barcode']))
            ws.write(i, 4, 1)
            ws.write(i, 5, barcode_type)
            ws.write(i, 6, target_location)
        wb.save(full_target)
        size = os.path.getsize(full_target)
        if size == 0:
            raise RuntimeError(f'Output file is 0 bytes: {full_target}')
        print(f'Created: {full_target} ({len(full_rows)} rows, {size} bytes)')
    else:
        print(f'[跳过] {full_target} 已存在且行数匹配（{len(full_rows)} 行）')


if __name__ == '__main__':
    main()
