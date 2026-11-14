// ensure-template.mjs —— 幂等保证第4步需要的移库导入空白模板 .xls 就位。
//
// 获取顺序（先界面、后直连、最后人工提示）：
//   0) 已在候选位置找到模板 -> 直接退出 0，不启动浏览器、不下载（幂等）。
//   1) 界面流程（主路径，复刻人工操作）：
//      登录 -> 左侧“库内处理/移库” -> 进入移库列表 -> 打开一张历史移库单进入详情页 move_edit.jsf
//      -> 点击“导入模板下载”按钮(#edit:excelmodel / input[value=导入模板下载]) -> 接收浏览器 download 事件落盘。
//      说明：模板内容来自系统静态文件，与具体单据无关，打开任意历史单即可，无需新建空单（避免产生半成品单据）。
//   2) 界面不可用时的直连兜底：复用登录态 GET 标准模板地址，校验 .xls(OLE2) 魔数后落盘。
//   3) 两者都失败：保存诊断并给出“界面手动点导入模板下载”的人工回退步骤，退出码 1。
// playwright 动态 import，模板已存在的跳过分支不加载浏览器模块。
import fs from 'node:fs/promises';
import path from 'node:path';
import {
  makeLogger,
  resolveBrowserExecutable,
  performLogin,
  saveDiagnostic,
  looksLikeXls,
} from './shared.mjs';

const root = path.resolve(process.argv[2] || '.');
const configPath = path.join(root, 'config.json');
const config = JSON.parse(await fs.readFile(configPath, 'utf8'));
const outputDir = path.join(root, '输出文件');
await fs.mkdir(outputDir, { recursive: true });
const diagDir = path.join(outputDir, 'diagnosis');
const { log } = makeLogger(outputDir, 'automation.log');

// 与 convert-xls 保持一致的候选顺序：config.templatePath → 项目 templates/ 目录
function candidatePaths() {
  const list = [];
  if (config.templatePath) list.push(path.resolve(root, String(config.templatePath)));
  list.push(path.join(root, 'templates', 'moveExcleModel.xls'));
  return [...new Set(list)];
}

async function existsFile(p) {
  try {
    await fs.access(p);
    return true;
  } catch {
    return false;
  }
}

// ---------- 界面导航（参照 system.mjs 的无副作用浏览动作） ----------

async function gotoMoveList(page) {
  await page.goto(config.systemUrl, { waitUntil: 'domcontentloaded', timeout: 60000 });
  const account = process.env.GWMS_ACCOUNT || config.account;
  const password = process.env.GWMS_PASSWORD || config.password;
  await performLogin(page, { account, password, log, diagDir, timeoutMs: 15000 });

  const left = page.frameLocator('#folderForm');
  let moves = left.getByText('移库', { exact: true });
  if (!(await moves.count())) {
    await left.getByText('库内处理', { exact: true }).click({ timeout: 10000 }).catch(() => {});
    for (let i = 0; i < 32; i += 1) {
      await page.waitForTimeout(250);
      if (await moves.count()) break;
    }
  }
  if ((await moves.count()) === 1) {
    const cls = await moves.first().getAttribute('class').catch(() => null);
    const onclick = await moves.first().getAttribute('onclick').catch(() => null);
    if (cls === 'folder' || onclick?.includes('getChildren')) {
      await moves.first().click({ timeout: 10000 }).catch(() => {});
      for (let i = 0; i < 20; i += 1) {
        await page.waitForTimeout(250);
        if ((await moves.count()) > 1) break;
      }
    }
  }
  if (!(await moves.count())) throw new Error('左侧菜单未找到“移库”节点（库内处理 -> 移库）');
  const last = moves.last();
  const anchor = last.locator('xpath=ancestor-or-self::a[1]');
  if (await anchor.count()) await anchor.click({ timeout: 10000 }).catch(() => {});
  else await last.click({ timeout: 10000 }).catch(() => {});
  await page.waitForTimeout(2500);
}

// 找到承载移库业务的工作区 Frame（move.jsf / move_edit.jsf 均含 /stocksys/move/）
async function activeMoveFrame(page) {
  for (let i = 0; i < 40; i += 1) {
    const frame = page.frames().find((f) => f.url().includes('/stocksys/move/'));
    if (frame) return frame;
    await page.waitForTimeout(250);
  }
  throw new Error('未进入移库工作区（/stocksys/move/）');
}

// 打开列表中第一张历史单据进入详情页；无历史单据时返回 null（不自动新建空单）
async function openFirstOrder(wf) {
  const links = wf.locator('a[id^="gtable_biid_"]');
  const count = await links.count();
  if (!count) return null;
  let pid = (await links.first().innerText().catch(() => '')).trim();
  if (!/^MV\d+$/.test(pid)) {
    const href = await links.first().getAttribute('href').catch(() => null);
    const m = href && href.match(/pid=([A-Za-z0-9_]+)/);
    if (m) pid = m[1];
  }
  if (!/^MV\d+$/.test(pid)) return null;
  await wf
    .goto(new URL(`move_edit.jsf?pid=${encodeURIComponent(pid)}`, wf.url()).toString(), {
      waitUntil: 'domcontentloaded',
      timeout: 30000,
    })
    .catch(() => {});
  await wf.locator('#edit\\:biid').waitFor({ timeout: 15000 }).catch(() => {});
  return pid;
}

// 在单据详情页点击“导入模板下载”，接收浏览器下载事件并保存
async function clickTemplateDownload(page, wf, target) {
  const btn = wf.locator('input[value="导入模板下载"], #edit\\:excelmodel').first();
  await btn.waitFor({ state: 'visible', timeout: 15000 });
  const [download] = await Promise.all([
    page.waitForEvent('download', { timeout: 30000 }),
    btn.click({ timeout: 15000 }),
  ]);
  await download.saveAs(target);
}

// 直连兜底：复用登录态 GET 标准模板地址
async function httpGetTemplate(page, target) {
  const origin = new URL(config.systemUrl).origin;
  const templateUrl = config.templateUrl || `${origin}/gwms_hsb/uplodexecl/moveExcleModel.xls`;
  const response = await page.request.get(templateUrl, {
    timeout: 60000,
    headers: { Referer: `${origin}/gwms_hsb/stocksys/move/move_edit.jsf` },
  });
  if (!response.ok()) throw new Error(`模板直连 HTTP ${response.status()}`);
  const body = Buffer.from(await response.body());
  if (!looksLikeXls(body)) throw new Error('直连响应不是有效的 .xls 模板');
  await fs.writeFile(target, body);
}

async function main() {
  // 0) 候选命中即跳过
  for (const candidate of candidatePaths()) {
    if (await existsFile(candidate)) {
      await log(`导入模板已存在，跳过自动获取: ${candidate}`);
      console.log(`[ensure-template] 模板已存在: ${candidate}`);
      return;
    }
  }

  // 落盘位置：显式 templatePath 优先，否则项目 templates/moveExcleModel.xls
  const target = config.templatePath
    ? path.resolve(root, String(config.templatePath))
    : path.join(root, 'templates', 'moveExcleModel.xls');
  await fs.mkdir(path.dirname(target), { recursive: true });

  const executablePath = await resolveBrowserExecutable(config);
  if (!executablePath) {
    throw new Error(
      '未找到 Edge/Chrome，无法自动获取模板；请在系统“移库单详情页”点“导入模板下载”，保存到 ' + target
    );
  }

  const { chromium } = await import('playwright');
  const profileDir = path.join(root, `browser-session-tpl-${Date.now()}`);
  const context = await chromium.launchPersistentContext(profileDir, {
    headless: !!config.headless,
    executablePath,
    acceptDownloads: true,
    viewport: { width: 1400, height: 900 },
  });

  let saved = false;
  let lastReason = '';
  try {
    const page = context.pages()[0] || (await context.newPage());

    // 1) 界面主路径
    try {
      await gotoMoveList(page);
      const wf = await activeMoveFrame(page);
      const pid = await openFirstOrder(wf);
      if (!pid) {
        lastReason = '移库列表没有可打开的历史单据，无法进入详情页点“导入模板下载”';
        await log(`[ensure-template] ${lastReason}，转直连兜底`);
      } else {
        await log(`[ensure-template] 在单据 ${pid} 详情页点击“导入模板下载”`);
        await clickTemplateDownload(page, wf, target);
        const bytes = await fs.readFile(target);
        if (looksLikeXls(bytes)) {
          await log(`[ensure-template] 界面下载成功: ${target}（${bytes.length} 字节，单据 ${pid}）`);
          console.log(`[ensure-template] 模板已通过界面下载: ${target}（${bytes.length} 字节）`);
          saved = true;
        } else {
          lastReason = '界面下载文件不是有效 .xls，已删除并转直连兜底';
          await fs.rm(target, { force: true }).catch(() => {});
          await log(`[ensure-template] ${lastReason}`);
        }
      }
    } catch (error) {
      lastReason = `界面下载失败：${error.message}`;
      await log(`[ensure-template] ${lastReason}，转直连兜底`);
      await saveDiagnostic(page, diagDir, 'template-ui-fail');
    }

    // 2) 直连兜底
    if (!saved) {
      try {
        await httpGetTemplate(page, target);
        const bytes = await fs.readFile(target);
        await log(`[ensure-template] 直连下载成功: ${target}（${bytes.length} 字节）`);
        console.log(`[ensure-template] 模板已直连下载: ${target}（${bytes.length} 字节）`);
        saved = true;
      } catch (error) {
        lastReason = `直连兜底失败：${error.message}`;
        await log(`[ensure-template] ${lastReason}`);
      }
    }

    if (!saved) {
      throw new Error(
        `自动获取模板失败（${lastReason}）。请手动处理：登录系统 -> 库内处理 -> 移库 -> 打开任一移库单 -> `
        + '点工具栏“导入模板下载”，将保存的文件放到 ' + target
        + '（或在 config.json 设置 templatePath）。'
      );
    }
  } finally {
    await context.close().catch(() => {});
  }
}

try {
  await main();
} catch (error) {
  const message = error && error.message ? error.message : String(error);
  console.error(`[ensure-template] 失败：${message}`);
  try {
    await log(`ensure-template ERROR: ${message}\n${error && error.stack ? error.stack : ''}`);
  } catch {
    // 日志写入失败不覆盖退出码
  }
  process.exit(1);
}
