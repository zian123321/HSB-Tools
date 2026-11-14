// check.mjs —— 启动预检（第一梯队新增）
// 用法: node check.mjs <项目根目录>
// 致命项 FAIL -> 退出码 1；可选项 WARN -> 仅提示；非 Windows 环境跳过 Windows 专有路径检查。
import fs from 'node:fs/promises';
import path from 'node:path';

const root = path.resolve(process.argv[2] || '.');
const isWin = process.platform === 'win32';
let fails = 0;
let warns = 0;
let oks = 0;

const ok = (m) => { oks += 1; console.log(`[OK]   ${m}`); };
const warn = (m) => { warns += 1; console.log(`[WARN] ${m}`); };
const fail = (m) => { fails += 1; console.log(`[FAIL] ${m}`); };
const skip = (m) => console.log(`[SKIP] ${m}`);

async function exists(p) {
  try { await fs.access(p); return true; } catch { return false; }
}

console.log(`预检目录: ${root}（${isWin ? 'Windows' : '非 Windows，专有路径自动跳过'}）\n`);

// 1. Node 版本
const major = Number(process.versions.node.split('.')[0]);
if (major >= 18) ok(`Node.js ${process.versions.node}`);
else fail(`Node.js 版本过低：${process.versions.node}，建议 >=18`);

// 2. config.json
const cfgPath = path.join(root, 'config.json');
let cfg = null;
if (await exists(cfgPath)) {
  try {
    cfg = JSON.parse(await fs.readFile(cfgPath, 'utf8'));
    ok('config.json 可正常解析');
  } catch (e) {
    fail(`config.json 解析失败：${e.message}`);
  }
} else {
  fail('缺少 config.json（首次运行 run.ps1 会从 config.example.json 自动复制）');
}

if (cfg) {
  const mustKeys = ['systemUrl', 'targetLocation', 'barcodeType'];
  const softKeys = ['warehouse', 'maxPerOrder'];
  for (const key of mustKeys) {
    if (cfg[key] === undefined || cfg[key] === '') fail(`config.${key} 未设置，无法完成下载/转换/建单`);
    else ok(`config.${key} = ${cfg[key]}`);
  }
  for (const key of softKeys) {
    if (cfg[key] === undefined || cfg[key] === '') warn(`config.${key} 未设置（可按默认或运行时确认）`);
    else ok(`config.${key} = ${cfg[key]}`);
  }
  if (!cfg.account && !process.env.GWMS_ACCOUNT) warn('账号未预置（将在交互提示处输入，属正常）');
  if (!cfg.password && !process.env.GWMS_PASSWORD) warn('密码未预置（将在交互提示处输入，属正常；不写盘）');
}

// 3. 源 Excel
if (cfg) {
  if (!cfg.sourceFile) {
    warn('sourceFile 为空：运行时会弹出 Excel 文件选择框');
  } else if (await exists(cfg.sourceFile)) {
    ok(`源 Excel 存在：${cfg.sourceFile}`);
  } else {
    warn(`sourceFile 指向的文件不存在，运行时会重新弹框选择：${cfg.sourceFile}`);
  }
}

// 4. 库存报表策略
if (cfg) {
  if (cfg.downloadInventoryAutomatically === false) {
    if (cfg.inventoryReport && await exists(cfg.inventoryReport)) ok(`按指定库存报表运行：${cfg.inventoryReport}`);
    else fail('downloadInventoryAutomatically=false，但 inventoryReport 为空或文件不存在；需手工指定库存明细报表.xlsx');
  } else if (cfg.downloadInventoryAutomatically === true) {
    ok('启用自动下载库存报表（登录成功后执行，并回写 inventoryReport）');
  } else {
    warn('downloadInventoryAutomatically 未设置：建议设 true 自动下载；若设 false，必须提供存在的 inventoryReport 文件');
  }
}

// 5. 浏览器（Windows 专有）
if (isWin) {
  const candidates = [
    cfg?.browserExecutable,
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  ].filter(Boolean);
  let browser = null;
  for (const c of candidates) { if (await exists(c)) { browser = c; break; } }
  if (browser) ok(`浏览器：${browser}`);
  else fail('未找到 Edge/Chrome；请在 config.browserExecutable 填入浏览器完整路径');

  // 6. playwright 本地依赖（node_modules 内）；缺失会导致 mjs 启动即 ERR_MODULE_NOT_FOUND
  const pw = path.join(root, 'node_modules', 'playwright', 'index.mjs');
  if (await exists(pw)) ok('playwright 本地模块存在');
  else warn('未检测到本地 playwright；请先在项目目录执行 npm install，否则启动会报 ERR_MODULE_NOT_FOUND');

  // 7. HAR 模板（可选，缺失已支持界面导出兜底）
  const har = cfg?.harFile || '';
  if (har && await exists(har)) ok(`HAR 查询/导出模板：${har}`);
  else warn('HAR 不存在：将自动走页面“导出”按钮兜底下载（无需 HAR 也可运行）');

  // 8. LibreOffice 与导入模板（第 4 步转换使用，缺失只 WARN 不阻断预检）
  const loPy = 'C:/Program Files/LibreOffice/program/python.exe';
  const soffice = 'C:/Program Files/LibreOffice/program/soffice.exe';
  const tplCandidates = [
    cfg?.templatePath,
    path.join(root, 'templates', 'moveExcleModel.xls'),
  ].filter(Boolean);
  let tpl = null;
  for (const c of tplCandidates) { if (await exists(c)) { tpl = c; break; } }
  if (await exists(loPy)) ok('LibreOffice python.exe 存在'); else warn('未检测到 LibreOffice python.exe，[4/5] xls 转换会失败（安装 LibreOffice 后可用）');
  if (await exists(soffice)) ok('soffice.exe 存在'); else warn('未检测到 soffice.exe');
  if (tpl) ok(`移库导入 xls 模板：${tpl}`);
  else warn('未检测到导入模板（已查 config.templatePath、templates/moveExcleModel.xls），[4/5] 转换会失败');
} else {
  skip('浏览器 / playwright / HAR / LibreOffice / xls 模板为 Windows 路径，当前环境不检查');
}

console.log('提示: 收尾 build-docs.py 依赖 python-docx、Pillow、reportlab；若文档生成失败可执行: python -m pip install python-docx pillow reportlab');

console.log(`\n预检结果: OK=${oks} WARN=${warns} FAIL=${fails}`);
if (fails > 0) {
  console.log('存在致命检查项 [FAIL]，请先修复再运行。');
  process.exit(1);
}
console.log('未发现致命项（WARN 为可选依赖，可按需处理）。');
process.exit(0);
