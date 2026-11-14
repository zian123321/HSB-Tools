import json, re, sys
from pathlib import Path
from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from PIL import Image, ImageDraw, ImageFont
from reportlab.graphics.barcode import code128

ROOT = Path(sys.argv[1]).resolve()
OUT = ROOT / '输出文件'
_raw = json.loads((OUT / 'chunks.json').read_text(encoding='utf-8'))
chunks = _raw['chunks'] if isinstance(_raw, dict) else _raw

# 读取配置：入库库位、条码类型（生成单据表单用）
config = {}
_cfg_file = ROOT / 'config.json'
if _cfg_file.exists():
    try:
        config = json.loads(_cfg_file.read_text(encoding='utf-8'))
    except Exception:
        config = {}
target_location = str(config.get('targetLocation') or '')
barcode_type = str(config.get('barcodeType') or '04')
chunk_by_name = {c['name']: c for c in chunks}

# 读取建单结果：切块名 -> 移库单号；created / success 只要拿到合法 MV 单号都用于出码
orders = []
orders_file = OUT / 'orders.json'
if orders_file.exists():
    try:
        orders = json.loads(orders_file.read_text(encoding='utf-8'))
    except Exception:
        orders = []
order_by_name = {}
for item in orders:
    oid = str(item.get('orderId') or '').strip()
    if re.fullmatch(r'MV\d+', oid):
        order_by_name[item.get('name', '')] = oid

def shade(cell, fill):
    shd=OxmlElement('w:shd'); shd.set(qn('w:fill'),fill); cell._tc.get_or_add_tcPr().append(shd)

def build_category_docs():
    import shutil
    from collections import defaultdict
    # 按品类分组：手机_1、手机_2 -> 手机；平板_1 -> 平板；其他_1 -> 其他
    groups = defaultdict(list)
    for chunk in chunks:
        # 品类名 = 切块名去掉 _数字 后缀
        cat = re.sub(r'_\d+$', '', chunk['name'])
        groups[cat].append(chunk)

    for cat, cat_chunks in groups.items():
        doc = Document()
        sec = doc.sections[0]
        sec.page_width = Cm(21); sec.page_height = Cm(29.7)
        sec.top_margin = sec.bottom_margin = Cm(1.5); sec.left_margin = sec.right_margin = Cm(1.5)

        # 标题
        total = sum(len(c['rows']) for c in cat_chunks)
        title = f"{cat} 移库条码表（共 {total} 条）"
        p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        r = p.add_run(title); r.bold = True; r.font.size = Pt(14)

        # 1) 先放所有移库单号的一维码
        for chunk in cat_chunks:
            oid = order_by_name.get(chunk['name'])
            if oid:
                bar_path = OUT / 'barcodes' / f"{oid}_Code128.png"
                if bar_path.exists():
                    bp = doc.add_paragraph(); bp.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    bp.add_run().add_picture(str(bar_path), width=Cm(14))
                    op = doc.add_paragraph(); op.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    orun = op.add_run(f"{oid}（{chunk['name']}）"); orun.bold = True; orun.font.size = Pt(12)
                    doc.add_paragraph()  # 空行隔开

        # 2) 再放商品条码表格（4列：条码、库位、条码、库位）
        for chunk in cat_chunks:
            records = chunk['rows']
            # 小标题
            sp = doc.add_paragraph()
            sr = sp.add_run(f"{chunk['name']}（{len(records)} 条）")
            sr.bold = True; sr.font.size = Pt(11)

            table = doc.add_table(rows=1, cols=4)
            table.alignment = WD_TABLE_ALIGNMENT.CENTER; table.autofit = False
            widths = [Cm(6.0), Cm(3.5), Cm(6.0), Cm(3.5)]
            for i, (c, t) in enumerate(zip(table.rows[0].cells, ['商品条码', '库位'] * 2)):
                c.width = widths[i]; c.text = t
                c.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER; shade(c, '1F4E78')
                c.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
                for rr in c.paragraphs[0].runs:
                    rr.bold = True; rr.font.size = Pt(9); rr.font.color.rgb = RGBColor(255, 255, 255)
            repeat = OxmlElement('w:tblHeader'); repeat.set(qn('w:val'), 'true')
            table.rows[0]._tr.get_or_add_trPr().append(repeat)

            n = (len(records) + 1) // 2
            for line in range(n):
                vals = []
                for block in range(2):
                    idx = line + block * n
                    vals += [records[idx]['barcode'], records[idx]['location']] if idx < len(records) else ['', '']
                cells = table.add_row().cells
                for col, (c, v) in enumerate(zip(cells, vals)):
                    c.width = widths[col]; c.text = str(v)
                    c.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
                    p = c.paragraphs[0]
                    p.alignment = WD_ALIGN_PARAGRAPH.LEFT if col % 2 == 0 else WD_ALIGN_PARAGRAPH.CENTER
                    p.paragraph_format.space_after = Pt(2); p.paragraph_format.line_spacing = 1.2
                    for rr in p.runs:
                        rr.font.size = Pt(8.5); rr.font.name = 'Arial'
                    if line % 2: shade(c, 'EDF4F8')

            doc.add_paragraph()  # 空行隔开不同切块

        # 保存 Word
        target = OUT / '条码文档' / f"{cat}.docx"; target.parent.mkdir(parents=True, exist_ok=True)
        try:
            doc.save(target)
        except PermissionError:
            import time
            ts = time.strftime('%H%M%S')
            alt = target.with_name(f"{target.stem}_{ts}{target.suffix}")
            doc.save(alt)
            print(f'File locked, saved as: {alt.name}')

        # 同时复制对应的 Excel 移库导入文件到 documents 目录
        for chunk in cat_chunks:
            xls_src = OUT / 'import_xls' / f"{chunk['name']}_移库导入.xls"
            if xls_src.exists():
                xls_dst = OUT / '条码文档' / f"{chunk['name']}_移库导入.xls"
                try:
                    shutil.copy2(xls_src, xls_dst)
                except PermissionError:
                    import time
                    ts = time.strftime('%H%M%S')
                    xls_dst = OUT / '条码文档' / f"{chunk['name']}_移库导入_{ts}.xls"
                    shutil.copy2(xls_src, xls_dst)

def _load_font(size):
    try:
        return ImageFont.truetype('C:/Windows/Fonts/arialbd.ttf', size)
    except Exception:
        return ImageFont.load_default()

def barcode_png(value,target):
    b=code128.Code128(value); b.validate(); b.encoded=b.encode(); pattern=b.decompose(); widths={'A':1,'B':2,'C':3,'D':4}; module=5; quiet=55
    w=quiet*2+sum(widths[x.upper()] for x in pattern)*module; im=Image.new('RGB',(w,470),'white'); d=ImageDraw.Draw(im); x=quiet
    for ch in pattern:
        seg=widths[ch.upper()]*module
        if ch.isupper(): d.rectangle((x,40,x+seg-1,350),fill='black')
        x+=seg
    # 字号自适应：保证单号文字不超出条码宽度
    size=62
    while size>=28:
        font=_load_font(size)
        box=d.textbbox((0,0),value,font=font)
        if (box[2]-box[0]) <= w-24: break
        size-=4
    font=_load_font(size)
    box=d.textbbox((0,0),value,font=font)
    d.text(((w-(box[2]-box[0]))/2,370),value,fill='black',font=font); im.save(target,'PNG',optimize=True)

# 1) 先为所有合法移库单号生成 Code128 一维码（不再按类别排除）
bar_dir=OUT/'barcodes'; bar_dir.mkdir(parents=True,exist_ok=True)
code_rows=[]
for item in orders:
    oid=str(item.get('orderId') or '').strip()
    if not re.fullmatch(r'MV\d+', oid):
        continue
    png_path = bar_dir / f"{oid}_Code128.png"
    # 幂等跳过：已有非空条码图则不再重复生成；缺失则补生成
    if not (png_path.exists() and png_path.stat().st_size > 0):
        barcode_png(oid, png_path)
    code_rows.append({
        'orderId': oid,
        'chunk': item.get('name',''),
        'category': item.get('category',''),
        'expected': item.get('expected',''),
        'status': item.get('status',''),
    })
code_rows.sort(key=lambda r: r['orderId'])
manifest=['移库单号\t切块文件\t类别\t预期条数\t状态']
for r in code_rows:
    manifest.append(f"{r['orderId']}\t{r['chunk']}\t{r['category']}\t{r['expected']}\t{r['status']}")
(bar_dir/'单号对照.txt').write_text('\n'.join(manifest)+'\n',encoding='utf-8')

# 2) 按品类合并 Word：手机类一个、平板类一个、其他类一个
build_category_docs()

# 3) 生成扫码核对工具 HTML（所有批次数据内嵌）
template_path = ROOT / '扫码核对工具_模板.html'
if template_path.exists():
    template = template_path.read_text(encoding='utf-8')
    # 收集所有批次的条码数据
    batches = []
    for chunk in chunks:
        batch = {
            'name': chunk['name'],
            'codes': [row['barcode'] for row in chunk['rows']]
        }
        batches.append(batch)
    # 内嵌 JSON
    import json as _json
    embed_js = f'window.__EMBEDDED_BATCHES__ = {_json.dumps(batches, ensure_ascii=False)};'
    html = template.replace('/* ================= 自动加载内嵌批次数据 ================= */', embed_js + '\n  /* ================= 自动加载内嵌批次数据 ================= */')
    # 输出到 条码文档 目录
    output_path = OUT / '条码文档' / '扫码核对工具.html'
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        output_path.write_text(html, encoding='utf-8')
        print(f'扫码核对工具已生成: {output_path.name} ({len(batches)} 个批次)')
    except PermissionError:
        import time
        ts = time.strftime('%H%M%S')
        alt = output_path.with_name(f'扫码核对工具_{ts}.html')
        alt.write_text(html, encoding='utf-8')
        print(f'文件被占用，另存为: {alt.name}')

# 兜底补齐：条码文档目录缺对应导入表时，从 import_xls 复制进去（单已建好但产物缺失的情况）
import shutil
doc_dir = OUT / '条码文档'
doc_dir.mkdir(parents=True, exist_ok=True)
for chunk in chunks:
    xls_src = OUT / 'import_xls' / f"{chunk['name']}_移库导入.xls"
    xls_dst = doc_dir / f"{chunk['name']}_移库导入.xls"
    if xls_src.exists() and not xls_dst.exists():
        try:
            shutil.copy2(xls_src, xls_dst)
            print(f'[补齐] 导入表缺失，已复制: {xls_dst.name}')
        except PermissionError:
            import time
            ts = time.strftime('%H%M%S')
            alt = doc_dir / f"{chunk['name']}_移库导入_{ts}.xls"
            shutil.copy2(xls_src, alt)
            print(f'[补齐] 文件被占用，另存为: {alt.name}')

# 全量对照表补齐：条码文档目录缺时从 import_xls 复制
full_src = OUT / 'import_xls' / '全部货品对照表.xls'
full_dst = doc_dir / '全部货品对照表.xls'
if full_src.exists() and not full_dst.exists():
    try:
        shutil.copy2(full_src, full_dst)
        print(f'[补齐] 全量对照表缺失，已复制: {full_dst.name}')
    except PermissionError:
        import time
        ts = time.strftime('%H%M%S')
        alt = doc_dir / f'全部货品对照表_{ts}.xls'
        shutil.copy2(full_src, alt)
        print(f'[补齐] 文件被占用，另存为: {alt.name}')

# 4) 按移库单号生成单据明细表单（本地切块+建单记录，离线可用）
try:
    import xlwt
except ImportError:
    import subprocess, sys as _sys
    subprocess.check_call([_sys.executable, '-m', 'pip', 'install', 'xlwt', '-q'])
    import xlwt

form_count = 0
for item in orders:
    oid = str(item.get('orderId') or '').strip()
    if not re.fullmatch(r'MV\d+', oid):
        continue
    chunk = chunk_by_name.get(item.get('name', ''))
    if not chunk or not chunk.get('rows'):
        continue
    wb = xlwt.Workbook(encoding='utf-8')
    ws = wb.add_sheet('Sheet1')
    headers = ['序号', '商品条码', '出库库位', '数量', '入库库位']
    for col, h in enumerate(headers):
        ws.write(0, col, h)
    for i, row in enumerate(chunk['rows'], start=1):
        ws.write(i, 0, i)
        ws.write(i, 1, str(row.get('barcode', '')))
        ws.write(i, 2, str(row.get('location', '')))
        ws.write(i, 3, 1)
        ws.write(i, 4, target_location)
    form_path = doc_dir / f"{oid}_移库表单.xls"
    try:
        wb.save(form_path)
        form_count += 1
        print(f'移库表单: {form_path.name}（{len(chunk["rows"])} 条）')
    except PermissionError:
        import time
        ts = time.strftime('%H%M%S')
        alt = doc_dir / f"{oid}_移库表单_{ts}.xls"
        wb.save(alt)
        form_count += 1
        print(f'文件被占用，另存为: {alt.name}')

print(f'documents/barcodes complete; order Code128 PNG = {len(code_rows)}; 移库表单 = {form_count}')
