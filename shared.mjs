// shared.mjs —— 统货下架工具公共模块（第一梯队优化新增）
// 职责：配置读取、日志、浏览器可执行文件统一探测、登录成功判定与诊断、
//       带退避重试的网络调用、失败现场截图/HTML 落盘。
// 本模块不直接 import playwright，浏览器实例由各主脚本传入，保持依赖解耦。
import fs from 'node:fs/promises';
import path from 'node:path';

export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// ---------- 配置与日志 ----------
export async function loadConfig(root) {
  const configPath = path.join(root, 'config.json');
  return JSON.parse(await fs.readFile(configPath, 'utf8'));
}

export function makeLogger(outDir, logName = 'automation.log') {
  const logPath = path.join(outDir, logName);
  const log = async (message) => {
    const line = `${new Date().toISOString()} ${message}`;
    console.log(line);
    try {
      await fs.appendFile(logPath, `${line}\n`, 'utf8');
    } catch {
      // 日志写盘失败不阻断主流程
    }
  };
  return { logPath, log };
}

// ---------- 浏览器可执行文件统一探测 ----------
// config.browserExecutable 优先，其后按常见安装位置依次探测；找不到返回 null。
export function browserCandidates(config) {
  return [
    config?.browserExecutable,
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  ].filter(Boolean);
}

export async function resolveBrowserExecutable(config) {
  for (const candidate of browserCandidates(config)) {
    try {
      await fs.access(candidate);
      return candidate;
    } catch {
      // 继续探测下一个
    }
  }
  return null;
}

// ---------- 通用重试（线性退避） ----------
export async function retry(fn, times = 2, baseDelay = 800) {
  let lastError;
  for (let attempt = 0; attempt <= times; attempt += 1) {
    try {
      return await fn(attempt);
    } catch (error) {
      lastError = error;
      if (attempt < times) await sleep(baseDelay * (attempt + 1));
    }
  }
  throw lastError;
}

// ---------- 登录相关 ----------
// 抓取登录页可见的错误提示文本（账号密码错误、锁定、验证码提示等），尽量把后端原因带进异常。
export async function visibleErrorText(page) {
  const selectors = [
    'div.error', 'span.error', '.errorMsg', '#errorMsg', '.alert', '.message',
    '#form1\\:msg', '#msg', '#errorMessage', '.login-error',
  ];
  const texts = [];
  for (const selector of selectors) {
    try {
      const locator = page.locator(selector);
      const count = await locator.count();
      for (let i = 0; i < Math.min(count, 3); i += 1) {
        const text = ((await locator.nth(i).innerText().catch(() => '')) || '').trim();
        if (text) texts.push(text);
      }
    } catch {
      // 选择器不匹配时忽略
    }
  }
  return [...new Set(texts)].join(' | ');
}

// 登录提交后等待“成功”：以可见密码输入框消失为准（比 count() 不区分可见性更可靠）。
export async function waitAfterLogin(page, { timeoutMs = 15000 } = {}) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const visiblePasswordCount = await page
      .locator('input[type=password]:visible')
      .count()
      .catch(() => 0);
    if (visiblePasswordCount === 0) return { ok: true };
    await sleep(300);
  }
  return { ok: false };
}

// 统一登录流程：填表 -> 点登录 -> 等待成功 -> 失败时收集弹窗/页面文案并落诊断。
// 成功时不抛错；失败时抛出带原因的 Error，便于上层写入 automation.log。
export async function performLogin(page, { account, password, log, diagDir, timeoutMs = 15000 }) {
  const visiblePasswordCount = await page
    .locator('input[name="form1:passWord"]:visible')
    .count()
    .catch(() => 0);
  if (!visiblePasswordCount) return { already: true };

  if (!account || !password) throw new Error('缺少账号或密码（config 与环境变量均为空，请在交互提示处输入）');

  await page.locator('input[name="form1:nv_userid"]:visible').first().fill(account);
  await page.locator('input[name="form1:passWord"]:visible').first().fill(password);
  await log?.(`正在登录（账号长度 ${String(account).length}）`);

  let dialogMessage = null;
  const dialogHandler = async (dialog) => {
    dialogMessage = dialog.message();
    await dialog.dismiss().catch(() => {});
  };
  page.on('dialog', dialogHandler);
  try {
    await retry(
      () => page.locator('input[name="form1:loginBtn"]:visible').first().click({ timeout: 10000 }),
      1,
      400,
    );
    const result = await waitAfterLogin(page, { timeoutMs });
    if (!result.ok) {
      const pageError = await visibleErrorText(page);
      await saveDiagnostic(page, diagDir, 'login-fail');
      const reason = dialogMessage || pageError || '登录后密码框仍在，未进入主界面（可能凭据错误、账号锁定、存在验证码或页面改版）';
      throw new Error(`登录失败：${reason}`);
    }
    if (dialogMessage) await log?.(`系统登录提示：${dialogMessage}`);
    return { already: false };
  } finally {
    page.off('dialog', dialogHandler);
  }
}

// ---------- 失败现场诊断：截图 + 页面 HTML ----------
export async function saveDiagnostic(page, dir, prefix = 'diag') {
  if (!dir) return;
  try {
    await fs.mkdir(dir, { recursive: true });
    await page.screenshot({ path: path.join(dir, `${prefix}.png`), fullPage: true }).catch(() => {});
    const html = await page.content().catch(() => '');
    if (html) await fs.writeFile(path.join(dir, `${prefix}.html`), html, 'utf8');
  } catch {
    // 诊断落盘失败不覆盖原始异常
  }
}

// ---------- 带重试的 HTTP GET（下载导出文件用） ----------
export async function getWithRetry(context, url, { retries = 2, timeoutMs = 120000 } = {}) {
  return retry(
    () =>
      context.request.get(url, { timeout: timeoutMs }).then((response) => {
        if (!response.ok()) throw new Error(`HTTP ${response.status()}`);
        return response;
      }),
    retries,
    1000,
  );
}

// 校验下载内容是否为有效的 xlsx（ZIP/OOXML 魔数 PK，且体量大于阈值）
export function looksLikeXlsx(bytes, minBytes = 1000) {
  return bytes.length >= minBytes && bytes[0] === 0x50 && bytes[1] === 0x4b;
}

// 校验下载内容是否为老式 BIFF/.xls（OLE2 复合文档魔数 D0 CF 11 E0 A1 B1 1A E1），
// 用于判断“导入模板下载”拿到的是不是真正的 Excel 97 模板，而不是 HTML 登录页/错误页。
export function looksLikeXls(bytes, minBytes = 512) {
  return bytes.length >= minBytes
    && bytes[0] === 0xd0 && bytes[1] === 0xcf
    && bytes[2] === 0x11 && bytes[3] === 0xe0
    && bytes[4] === 0xa1 && bytes[5] === 0xb1
    && bytes[6] === 0x1a && bytes[7] === 0xe1;
}
