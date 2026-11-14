import fs from 'node:fs/promises';
import path from 'node:path';
import { chromium } from 'playwright';
import {
  makeLogger,
  resolveBrowserExecutable,
  performLogin,
  retry,
  getWithRetry,
  looksLikeXlsx,
  saveDiagnostic,
  sleep,
} from './shared.mjs';

const root = path.resolve(process.argv[2] || '.');
const configPath = path.join(root, 'config.json');
const config = JSON.parse(await fs.readFile(configPath, 'utf8'));
const outputDir = path.join(root, '输出文件');
await fs.mkdir(outputDir, { recursive: true });
const diagDir = path.join(outputDir, 'diagnosis');
const { log } = makeLogger(outputDir, 'automation.log');

// 库存报表每次运行都重新下载最新版，不沿用本地旧报表（覆盖写 库存明细报表.xlsx）。

const account = process.env.GWMS_ACCOUNT || config.account;
const password = process.env.GWMS_PASSWORD || config.password;

// HAR 抓包模板：config.harFile 优先；缺失或模板不完整不再直接崩溃，降级为界面按钮导出。
const harPath = config.harFile || '';
let queryEntry = null;
let exportEntry = null;
try {
  const har = JSON.parse(await fs.readFile(harPath, 'utf8'));
  const entries = har?.log?.entries || [];
  queryEntry = entries.find(
    (e) => e.request?.method === 'POST'
      && /stockSearch\.jsf(?:$|\?)/.test(e.request.url)
      && !e.request.url.includes('cache=true')
      && (e.request.postData?.text || '').includes('javax.faces.ViewState'),
  );
  exportEntry = entries.find(
    (e) => e.request?.method === 'POST'
      && e.request.url.includes('stockSearch.jsf')
      && e.request.url.includes('cache=true')
      && (e.request.postData?.text || '').includes('isExport=true'),
  );
  if (!queryEntry || !exportEntry) await log('HAR 缺少完整查询/导出模板，将改用界面按钮导出兜底');
} catch {
  await log(`未找到或无法解析 HAR（${harPath}），将改用界面按钮导出兜底`);
}

const executablePath = await resolveBrowserExecutable(config);
if (!executablePath) throw new Error('未找到 Edge 或 Chrome 浏览器；可在 config.json 的 browserExecutable 指定完整路径');

// 全程后端运行：强制 headless，不弹出浏览器窗口。
const context = await chromium.launchPersistentContext(path.join(root, `browser-session-${Date.now()}`), {
  headless: true,
  executablePath,
  acceptDownloads: true,
});
const page = context.pages()[0] || await context.newPage();

function replaceViewState(body, viewState) {
  const encoded = encodeURIComponent(viewState);
  if (!/javax\.faces\.ViewState=[^&]*/.test(body)) throw new Error('请求模板缺少 ViewState');
  return body.replace(/javax\.faces\.ViewState=[^&]*/, `javax.faces.ViewState=${encoded}`);
}

function extractViewState(html) {
  const match = html.match(/name=["']javax\.faces\.ViewState["'][^>]*value=["']([^"']+)["']/i)
    || html.match(/value=["']([^"']+)["'][^>]*name=["']javax\.faces\.ViewState["']/i);
  return match?.[1]?.replaceAll('&amp;', '&');
}

async function loginOnce() {
  await page.goto(config.systemUrl, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await performLogin(page, { account, password, log, diagDir, timeoutMs: 15000 });
}

// 快路径：沿用 HAR 模板，替换实时 ViewState 后 POST 查询/导出，再下载临时 xlsx。
async function downloadViaHar(initialViewState) {
  await log('提交库存查询请求');
  const queryResult = await retry(async () => page.evaluate(async ({ url, body }) => {
    const response = await fetch(url, {
      method: 'POST',
      credentials: 'include',
      headers: { 'content-type': 'application/x-www-form-urlencoded' },
      body,
    });
    return { status: response.status, ok: response.ok, text: await response.text() };
  }, { url: queryEntry.request.url, body: replaceViewState(queryEntry.request.postData.text, initialViewState) }), 2, 1000);
  if (!queryResult.ok) throw new Error(`库存查询失败，HTTP ${queryResult.status}`);
  const latestViewState = extractViewState(queryResult.text) || initialViewState;

  await log('提交库存导出请求');
  const exportBody = /javax\.faces\.ViewState=/.test(exportEntry.request.postData.text)
    ? replaceViewState(exportEntry.request.postData.text, latestViewState)
    : exportEntry.request.postData.text;
  const exportResult = await retry(async () => page.evaluate(async ({ url, body }) => {
    const response = await fetch(url, {
      method: 'POST',
      credentials: 'include',
      redirect: 'follow',
      headers: { 'content-type': 'application/x-www-form-urlencoded' },
      body,
    });
    return { status: response.status, ok: response.ok, url: response.url, text: await response.text() };
  }, { url: exportEntry.request.url, body: exportBody }), 2, 1000);
  if (!exportResult.ok) throw new Error(`库存导出失败，HTTP ${exportResult.status}`);

  const text = `${exportResult.url || ''}\n${exportResult.text}`;
  const match = text.match(/(?:https?:\/\/[^\s"'<>]+)?\/gwms_hsb\/ExportFiles\/[^\s"'<>]+\.xlsx/i)
    || text.match(/ExportFiles\/[^\s"'<>]+\.xlsx/i)
    || text.match(/href=["']([^"']+\.xlsx)["']/i);
  if (!match) throw new Error('导出接口未返回 Excel 临时文件地址（将回退界面导出）');

  const fileUrl = new URL(match[0], config.systemUrl).toString();
  await log(`下载库存报表: ${fileUrl}`);
  const fileResponse = await getWithRetry(context, fileUrl);
  const bytes = await fileResponse.body();
  if (!looksLikeXlsx(bytes)) throw new Error('下载内容不是有效的 XLSX 文件');

  const target = path.join(outputDir, '库存明细报表.xlsx');
  await fs.writeFile(target, bytes);
  await log(`库存报表下载成功，共 ${bytes.length} 字节`);
  return target;
}

// 兜底路径：HAR 不可用时，在库存查询页直接点“导出”按钮接收下载文件。
async function downloadViaUi() {
  const queryBtn = page.locator('input[value*="查询"],input[value*="搜索"],button:has-text("查询"),button:has-text("搜索")').first();
  if (await queryBtn.count().catch(() => 0)) {
    await queryBtn.click({ timeout: 10000 }).catch(() => {});
    await sleep(1500);
  }
  const exportBtn = page.locator('input[value*="导出"],button:has-text("导出"),a:has-text("导出")').first();
  if (!await exportBtn.count().catch(() => 0)) {
    await saveDiagnostic(page, diagDir, 'no-export-button');
    throw new Error('库存查询页面未找到导出按钮；可手工下载库存明细报表.xlsx，在 config.json 设置 inventoryReport 并把 downloadInventoryAutomatically 改为 false');
  }
  const downloadPromise = page.waitForEvent('download', { timeout: 60000 });
  await exportBtn.click();
  const download = await downloadPromise;
  const target = path.join(outputDir, '库存明细报表.xlsx');
  await download.saveAs(target);
  const bytes = await fs.readFile(target);
  if (!looksLikeXlsx(bytes)) throw new Error('界面下载内容不是有效的 XLSX 文件');
  await log(`界面导出下载成功，共 ${bytes.length} 字节`);
  return target;
}

try {
  await log('开始后台登录');
  const loginRetries = Number(config.loginRetries ?? 2);
  await retry(loginOnce, loginRetries, 1500);
  await log('后台登录成功');

  const stockUrl = new URL('/gwms_hsb/report/staReport/stockSearch.jsf?ch_moduleid=STOCKSEARCH&url=../report/staReport/stockSearch.jsf&isAll=0', config.systemUrl).toString();
  await page.goto(stockUrl, { waitUntil: 'domcontentloaded', timeout: 60000 });
  const initialViewState = await page.locator('input[name="javax.faces.ViewState"]').inputValue().catch(() => null);

  let savedTarget;
  if (queryEntry && exportEntry && initialViewState) {
    savedTarget = await downloadViaHar(initialViewState);
  } else {
    await log('使用界面导出按钮兜底下载');
    savedTarget = await downloadViaUi();
  }

  config.inventoryReport = savedTarget;
  await fs.writeFile(configPath, JSON.stringify(config, null, 2), 'utf8');
} catch (error) {
  await log(`ERROR ${error.stack || error}`);
  await saveDiagnostic(page, diagDir, 'download-fail');
  throw error;
} finally {
  await context.close();
}
