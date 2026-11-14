import fs from 'node:fs/promises';
import path from 'node:path';
import { chromium } from 'playwright';
import { resolveBrowserExecutable, performLogin, saveDiagnostic } from './shared.mjs';

const root=path.resolve(process.argv[2]||'.'); const mode=process.argv[3]||'orders';
const config=JSON.parse(await fs.readFile(path.join(root,'config.json'),'utf8'));
const outDir=path.join(root,'输出文件'); await fs.mkdir(outDir,{recursive:true});
const logFile=path.join(outDir,'automation.log');
const log=async m=>{const line=`${new Date().toISOString()} ${m}`; console.log(line); await fs.appendFile(logFile,line+'\n','utf8');};
const executablePath=await resolveBrowserExecutable(config);
if(!executablePath) throw new Error('未找到 Edge 或 Chrome 浏览器；可在 config.json 的 browserExecutable 指定完整路径');
const diagDir=path.join(outDir,'diagnosis');
const context=await chromium.launchPersistentContext(path.join(root,'浏览器数据_运行_'+Date.now()),{headless:true,executablePath,acceptDownloads:true,viewport:{width:1400,height:900}});
const page=context.pages()[0]||await context.newPage();
let lastDialogMessage='';
const popups=[];
context.on('page',p=>popups.push(p));
page.on('dialog',async dialog=>{lastDialogMessage=dialog.message(); await log(`系统提示：${lastDialogMessage}`); await dialog.dismiss().catch(()=>{});});

async function login(){
  await page.goto(config.systemUrl,{waitUntil:'domcontentloaded'});
  if(await page.locator('input[type=password]').count()){
    const account=process.env.GWMS_ACCOUNT||config.account;
    const password=process.env.GWMS_PASSWORD||config.password;
    await performLogin(page,{account,password,log,diagDir,timeoutMs:15000});
  }
  if(await page.locator('input[type=password]:visible').count()) throw new Error('登录失败，系统仍停留在登录页');
  if(!page.url().includes('main.jsf')) throw new Error('登录失败，请检查账号密码');
  await log('后台登录成功');
}
async function left(){return page.frameLocator('#folderForm')}
async function currentWorkFrame(){
  const frames=await page.locator('iframe').evaluateAll(xs=>xs.filter(x=>x.id!=='folderForm'&&x.id!=='bannerForm'&&x.offsetWidth>100&&x.offsetHeight>100).map(x=>({id:x.id,name:x.name,src:x.src,width:x.offsetWidth,height:x.offsetHeight})));
  if(!frames.length) throw new Error('找不到系统工作区');
  const target=frames.find(x=>x.id.startsWith('tab')&&x.src.includes('/stocksys/')) || frames.at(-1);
  await log(`工作区: ${target.id||target.name} ${target.src}`);
  let actual;
  for(let i=0;i<40;i++){
    actual=page.frames().find(f=>f.name()===(target.name||target.id)&&f.url().includes('/stocksys/')) || page.frames().find(f=>f.url().includes('/stocksys/move/'));
    if(actual) break;
    await page.waitForTimeout(250);
  }
  if(!actual) throw new Error(`未取得实际工作区 Frame: ${target.name||target.id}`);
  return actual;
}
async function downloadInventory(){
  await login();
  await page.goto(new URL('/gwms_hsb/report/staReport/stockSearch.jsf?ch_moduleid=STOCKSEARCH&url=../report/staReport/stockSearch.jsf&isAll=0',config.systemUrl).toString(),{waitUntil:'domcontentloaded'});
  await page.waitForTimeout(1200);
  const harPath = config.harFile || '';
  let har=null;
  if(harPath && await fs.access(harPath).then(()=>true).catch(()=>false)){
    har=JSON.parse(await fs.readFile(harPath,'utf8'));
    const q=har.log.entries.find(e=>e.request.method==='POST'&&e.request.url.includes('stockSearch.jsf')&&!e.request.url.includes('cache=true'));
    const vs=await page.locator('input[name="javax.faces.ViewState"]').inputValue().catch(()=>null);
    if(q?.request?.postData?.text&&vs){
      const body=q.request.postData.text.replace(/javax\.faces\.ViewState=[^&]*/,'javax.faces.ViewState='+encodeURIComponent(vs));
      await page.request.post(q.request.url,{headers:{'content-type':'application/x-www-form-urlencoded'},data:body});
      await page.reload({waitUntil:'domcontentloaded'}); await page.waitForTimeout(1800);
    }
  }
  // JSF 后端导出需要动态 ViewState；先确保登录会话中获取到该令牌。
  // 查询请求已经在当前库存页面会话中提交，不再重复跳转覆盖 ViewState。
  await page.waitForTimeout(1200);
  const exportReq=har?.log?.entries?.find(e=>e.request.method==='POST'&&e.request.url.includes('cache=true'));
  if(exportReq?.request?.postData?.text){
    const currentVs=await page.locator('input[name="javax.faces.ViewState"]').inputValue().catch(()=>null);
    let exportBody=exportReq.request.postData.text;
    if(currentVs) exportBody=exportBody.replace(/javax\.faces\.ViewState=[^&]*/,'javax.faces.ViewState='+encodeURIComponent(currentVs));
    const resp=await page.request.post(exportReq.request.url,{headers:{'content-type':'application/x-www-form-urlencoded'},data:exportBody});
    const txt=await resp.text(); const m=txt.match(/(?:href|src)=["']([^"']*ExportFiles[^"']+\.xlsx)["']/i);
    if(m){const u=new URL(m[1],config.systemUrl).toString(); const file=await page.request.get(u); const target=path.join(outDir,'库存明细报表.xlsx'); await fs.writeFile(target,await file.body()); config.inventoryReport=target; await fs.writeFile(path.join(root,'config.json'),JSON.stringify(config,null,2),'utf8'); await log(`库存报表已下载: ${target}`); return;}
  }
  const directExportBtn=page.locator('input[value*="导出"],button:has-text("导出"),a:has-text("导出")').first();
  if(!await directExportBtn.count()) throw new Error('库存查询页面未找到导出按钮');
  const directDownload=page.waitForEvent('download',{timeout:60000}); await directExportBtn.click();
  const directFile=await directDownload; const directTarget=path.join(outDir,'库存明细报表.xlsx'); await directFile.saveAs(directTarget);
  config.inventoryReport=directTarget; await fs.writeFile(path.join(root,'config.json'),JSON.stringify(config,null,2),'utf8'); await log(`库存报表已下载: ${directTarget}`); return;
  const candidates=['库存明细报表','库存明细','库存报表']; let clicked=false;
  for(const name of candidates){const loc=lf.getByText(name,{exact:true}); if(await loc.count()){await loc.last().click(); clicked=true; break;}}
  if(!clicked) throw new Error('未找到库存明细报表菜单；可在config.json填写inventoryReport后重跑');
  await page.waitForTimeout(1500); const wf=page;
  for(const text of ['实时库位明细','库内可售机器库存情况']){const loc=wf.getByText(text,{exact:false}); if(await loc.count()) await loc.first().click();}
  const downloadPromise=page.waitForEvent('download',{timeout:60000});
  const exportBtn=wf.getByText(/导出\s*Excel|导出Excel/,{exact:false}).or(wf.locator('input[value*="导出"]')).first(); await exportBtn.click();
  const download=await downloadPromise; const target=path.join(outDir,'库存明细报表.xlsx'); await download.saveAs(target);
  config.inventoryReport=target; await fs.writeFile(path.join(root,'config.json'),JSON.stringify(config,null,2),'utf8'); await log(`库存报表已下载 ${target}`);
}
async function openMove(){
  // 先重置：如果当前停留在编辑页/错误页，先导航回主框架，避免基于坏状态操作
  try{
    const mainUrl = page.url();
    if (/move_edit\.jsf|move_add\.jsf|error|500/.test(mainUrl) || !/main\.jsf/.test(mainUrl)){
      await page.goto(config.systemUrl, {waitUntil:'domcontentloaded'});
      await page.waitForTimeout(1500);
    }
  }catch{}
  const lf=await left(); let moves=lf.getByText('移库',{exact:true}); let count=await moves.count();
  if(!count){
    await lf.getByText('库内处理',{exact:true}).click();
    for(let i=0;i<32;i++){await page.waitForTimeout(250); count=await moves.count(); if(count) break;}
  }
  if(count===1){
    const cls=await moves.first().getAttribute('class').catch(()=>null);
    const onclick=await moves.first().getAttribute('onclick').catch(()=>null);
    if(cls==='folder'||onclick?.includes('getChildren')){
      await moves.first().click();
      for(let i=0;i<20;i++){await page.waitForTimeout(250); count=await moves.count(); if(count>1) break;}
    }
  }
  await log(`发现 ${count} 个“移库”菜单节点`);
  if(!count) throw new Error('未找到移库子菜单');
  const moveNode=await moves.last().evaluate(el=>({tag:el.tagName,html:el.outerHTML,parent:el.parentElement?.outerHTML,anchor:el.closest('a')?.outerHTML}));
  await log(`移库节点结构: ${JSON.stringify(moveNode)}`);
  const anchor=moves.last().locator('xpath=ancestor-or-self::a[1]');
  if(await anchor.count()) await anchor.click(); else await moves.last().click();
  await page.waitForTimeout(2500);
  const frameNodes=await page.locator('iframe').evaluateAll(xs=>xs.map(x=>({id:x.id,name:x.name,src:x.getAttribute('src'),display:getComputedStyle(x).display,width:x.offsetWidth,height:x.offsetHeight})));
  await log(`页面 iframe: ${JSON.stringify(frameNodes)}`);
}
async function openOrder(orderId){
  const wf=await currentWorkFrame();
  const editUrl=new URL(`move_edit.jsf?pid=${encodeURIComponent(orderId)}`,wf.url()).toString();
  await wf.goto(editUrl,{waitUntil:'domcontentloaded'});
  try{
    await wf.locator('#edit\\:biid').waitFor({timeout:15000});
  }catch(e){
    let info={};
    try{
      info={url:wf.url(),biidCount:await wf.locator('#edit\\:biid').count(),biidVisible:await wf.locator('#edit\\:biid').isVisible().catch(()=>false),bodyText:(await wf.locator('body').allTextContents().catch(()=>[])).join('').slice(0,300),listIds:await wf.locator('a[id^="gtable_biid_"]').evaluateAll(els=>els.map(x=>String(x.textContent||x.getAttribute('value')||'').trim()).filter(Boolean)).catch(()=>[])};
    }catch{}
    try{await fs.mkdir(diagDir,{recursive:true}); await fs.writeFile(path.join(diagDir,'open-fail.json'),JSON.stringify({orderId,editUrl,...info},null,2),'utf8');}catch{}
    const err=new Error(`打开移库单 ${orderId} 失败：编辑页单号框 #edit:biid 15秒未出现（当前URL=${info.url||'?'}，框存在=${info.biidCount||0}）。该断点单可能无效/已删除/被重定向，已自动清除断点并新建干净单。详见 diagnosis/open-fail.json`);
    err.needsReopen=true; err.staleOrderId=orderId;
    throw err;
  }
  const actual=await wf.locator('#edit\\:biid').inputValue();
  if(actual!==orderId) throw new Error(`打开移库单失败：期望 ${orderId}，实际 ${actual}`);
  return wf;
}
async function orderRecordCount(wf){
  const value=await wf.locator('#gtable_records').inputValue().catch(()=>'');
  const count=Number(value);
  return Number.isFinite(count)?count:0;
}
// 从文本/属性中抽取首个 MV 单号（兼容链接 value 属性与带空格/中文的 innerText）
const extractMv = (text) => { const m = String(text || '').match(/MV\d+/); return m ? m[0] : ''; };
// MV 单号按数字大小排序，取编号最大者（系统单号随时间递增，刚建的单一定最大）
const mvNum = (mv) => parseInt(String(mv||'').replace(/^MV/, ''), 10) || 0;
const pickNewestMv = (arr) => { const list = (arr||[]).filter(Boolean).slice(); list.sort((a,b)=>mvNum(b)-mvNum(a)); return list[0] || ''; };
async function listOrderIds(wf){
  // 注意：evaluateAll 回调在浏览器页面内执行，不能引用 Node 侧函数/变量，必须自包含；
  // 单号可能在 value 属性、文本或 href(pid=MV...) 中，三来源合并后内联正则抽取。
  const vals = await wf.locator('a[id^="gtable_biid_"]').evaluateAll(
    els => els.map(e => {
      const raw = [e.getAttribute('value'), e.textContent, e.getAttribute('href')].filter(Boolean).join(' ');
      const m = String(raw).match(/MV\d+/);
      return m ? m[0] : '';
    }).filter(Boolean)
  );
  return [...new Set(vals)];
}
async function readBiid(wf){ return extractMv(await wf.locator('#edit\\:biid').inputValue().catch(() => '')); }
async function readEditMsg(wf){
  const v = await wf.locator('#edit\\:msg').inputValue().catch(() => '');
  const t = await wf.locator('#edit\\:msg').innerText().catch(() => '');
  return String(v || t || '').trim();
}
async function clickFirstVisible(wf, selectors){
  for (const sel of selectors){
    try{
      const loc = wf.locator(sel);
      if (await loc.count() === 0) continue;
      const first = loc.first();
      try{
        await first.click({ timeout: 8000 }); return sel;
      }catch{
        // 仅对“带 onclick(A4J/提交动作) 或 type=submit”的隐藏元素降级 JS 点击；无动作的隐藏占位直接跳过，避免假点击
        const actionable = await first.evaluate(el => !!(el.getAttribute('onclick') || el.getAttribute('type') === 'submit')).catch(() => false);
        if (!actionable) continue;
        await first.evaluate(el=>el.click()).catch(()=>{});
        return sel + '(js)';
      }
    }catch{ /* 尝试下一个选择器 */ }
  }
  return null;
}
// 统计每个候选“真实可见(占布局)”数量，与存在数区分，避免把 display:none 误当可用
async function buttonVisibleCounts(wf, selectors){
  const out = {};
  for (const s of selectors){
    try{
      out[s] = await wf.locator(s).evaluateAll(xs => xs.filter(x => (x.offsetParent || x.getBoundingClientRect().width > 0) && getComputedStyle(x).display !== 'none').length).catch(()=>0);
    }catch{ out[s] = -1; }
  }
  return out;
}
async function readField(wf, sel){
  const v = await wf.locator(sel).inputValue().catch(() => '');
  if (v !== undefined && v !== '') return v;
  return (await wf.locator(sel).innerText().catch(() => '') || '').trim();
}
// 跨全部 frame + 弹窗 page 收集 MV 单号集合（浏览器回调只返回纯字符串，单号在 Node 侧统一正则抽取，避免作用域错误）
async function globalMvSet(){
  const set = new Set();
  const sources = [...page.frames(), ...popups];
  for (const src of sources){
    let text = src.url();
    try{ text += ' ' + await src.evaluate(() => { try{ return document.body ? document.body.innerText : ''; }catch{ return ''; } }); }catch{}
    for (const m of String(text).matchAll(/MV\d+/g)) set.add(m[0]);
  }
  return [...set];
}
// 新建页 move_add.jsf 与编辑页 move_edit.jsf 的保存按钮 id 可能不同，统一候选（明确保存语义，排除“添加单据”这类再新增按钮）
const SAVE_CANDIDATES = [
  '#edit\\:addId', '#edit\\:updateId', '#edit\\:saveAdd', '#edit\\:addBut', '#edit\\:submitAdd',
  'input[value="保存单据"]', 'input[value="保存"]', 'input[value="确定"]', 'input[value="新增"]'
];
// 定位新建/编辑表单：URL 为 move_add/move_edit，且含仓库字段或任一保存候选；不再死认编辑页 #edit:updateId
async function locateEditContext(){
  const sources = [...page.frames(), ...popups];
  for (const src of sources){
    try{
      const url = src.url();
      if (!/move_(add|edit)\.jsf/.test(url)) continue;
      const hasWh = await src.locator('#edit\\:whna, #edit\\:whid').count().catch(() => 0);
      const hasSave = await src.locator(SAVE_CANDIDATES.join(',')).count().catch(() => 0);
      // 只要 URL 进入 move_add/move_edit 即接管容器；字段/按钮是否齐备交由保存候选计数与诊断呈现，避免因渲染先后漏掉新建页
      return { kind: url.includes('move_add') ? 'add' : 'edit', obj: src, url, hasWh, hasSave };
    }catch{}
  }
  return null;
}
async function waitEditContext(timeoutMs = 15000){
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs){
    const ec = await locateEditContext();
    if (ec) return ec;
    await page.waitForTimeout(300);
  }
  return null;
}
async function probeAllCounts(){
  const out = [];
  for (const f of page.frames()){
    const counts = {};
    for (const s of SAVE_CANDIDATES){ counts[s] = await f.locator(s).count().catch(() => -1); }
    out.push({ kind: 'frame', name: f.name(), url: f.url(),
      pageType: /move_add\.jsf/.test(f.url()) ? 'add' : (/move_edit\.jsf/.test(f.url()) ? 'edit' : 'list'),
      whna: await f.locator('#edit\\:whna').count().catch(() => -1),
      addHead: await f.locator('#edit\\:addHead').count().catch(() => -1), saveCandidates: counts });
  }
  for (const p of popups){
    const counts = {};
    for (const s of SAVE_CANDIDATES){ counts[s] = await p.locator(s).count().catch(() => -1); }
    out.push({ kind: 'popup', url: p.url(),
      pageType: /move_add\.jsf/.test(p.url()) ? 'add' : (/move_edit\.jsf/.test(p.url()) ? 'edit' : 'list'),
      whna: await p.locator('#edit\\:whna').count().catch(() => -1),
      saveCandidates: counts });
  }
  return out;
}
async function buttonCounts(wf, selectors){
  const out = {};
  for (const s of selectors){ try{ out[s] = await wf.locator(s).count(); }catch{ out[s] = -1; } }
  return out;
}
// 选择仓库：先点触发器（图标/onclick 优先，文本框兜底），再从主文档/各 iframe/弹窗中精确点选；回传 whid/whna 便于校验
async function selectWarehouse(wf, steps){
  const wanted = config.warehouse;
  // 只保留会弹出选择弹窗的触发器，点文本框(#edit:whna)没用
  const triggerSels = ['#whid_img', 'img[id*=whid]', '[onclick*=whid]', '[onclick*=Whid]', 'input[onclick*=whid]', 'span[id*=whid]', 'a[id*=whid]'];
  let triggered = null;
  for (const s of triggerSels){
    try{
      if (await wf.locator(s).count() > 0 && await wf.locator(s).first().isVisible().catch(()=>false)){
        await wf.locator(s).first().click({ timeout: 6000 });
        triggered = s;
        steps.push(`仓库触发器命中 ${s}`);
        await page.waitForTimeout(1000);
        // 点完后检查 waho 弹窗是否出现，没出现就换一个触发器
        let wahoFound = false;
        for (const f of page.frames()){ if (/waho\.jsf/.test(f.url())) { wahoFound = true; break; } }
        if (wahoFound) break;
        steps.push(`触发器 ${s} 未弹出 waho 弹窗，换下一个`);
      }
    }catch(e){ steps.push(`触发器 ${s} 失败: ${e.message.slice(0,140)}`); }
  }
  let chosen = false, how = '';
  if (wanted){
    // 0) waho 选择器弹窗：表格行点击（老 JSF waho.jsf 弹窗里点行才生效）
    for (const f of page.frames()){
      try{
        const url = f.url();
        if (!/waho\.jsf/.test(url)) continue;
        // 在 waho 弹窗里找包含仓库名的表格行
        const rows = f.locator('tr').filter({ hasText: wanted });
        const rowCount = await rows.count();
        if (rowCount > 0){
          await rows.first().click({ timeout: 5000 });
          chosen = true; how = 'waho-row@' + (f.name() || 'waho');
          steps.push(`选中仓库 ${wanted}(${how})`);
          await page.waitForTimeout(800);
          break;
        }
      }catch(e){ steps.push(`waho弹窗点行失败: ${e.message.slice(0,100)}`); }
    }
    // 1) 文本精确命中（主文档/各 frame/弹窗）
    if (!chosen){
      const sources = [];
      try{ sources.push({ loc: page.getByText(wanted, { exact: true }), where: 'main' }); }catch{}
      for (const f of page.frames()){ try{ sources.push({ loc: f.getByText(wanted, { exact: true }), where: 'frame:' + (f.name() || f.url().slice(-24)) }); }catch{} }
      for (const p of popups){ try{ sources.push({ loc: p.getByText(wanted, { exact: true }), where: 'popup' }); }catch{} }
      for (const src of sources){
        try{ if (await src.loc.count() > 0){ await src.loc.first().click({ timeout: 6000 }); chosen = true; how = 'text-exact@' + src.where; steps.push(`选中仓库 ${wanted}(${how})`); break; } }catch{}
      }
    }
    // 2) 下拉 select option 兜底：精确 label 优先，再包含匹配，最后按 index
    if (!chosen){
      for (const f of [wf, ...page.frames(), ...popups]){
        try{
          const sels = await f.locator('select').evaluateAll(els => els.map(el => ({ name: el.name, id: el.id, options: [...el.options].map(o => ({ v: o.value, t: (o.textContent || '').trim() })) })));
          const hit = sels.find(s => s.options.some(o => o.t === wanted)) || sels.find(s => s.options.some(o => o.t && o.t.includes(wanted)));
          if (hit){
            const loc = hit.name ? f.locator('select[name="' + hit.name + '"]').first() : f.locator('select').first();
            await loc.selectOption({ label: wanted }).catch(async() => {
              const idx = hit.options.findIndex(o => o.t && o.t.includes(wanted));
              if (idx >= 0) await loc.selectOption({ index: idx });
            });
            chosen = true; how = 'select-option@' + (hit.name || hit.id || 'select'); steps.push(`下拉选中仓库 ${wanted}(${how})`); break;
          }
        }catch{}
      }
    }
    if (!chosen) steps.push(`未命中仓库“${wanted}”(文本/下拉均未中；真实候选见诊断 warehouseCandidates)`);
  }
  await page.waitForTimeout(500);
  // 不重置回主 frame：编辑表单可能位于子 frame 或弹窗，保持当前编辑容器
  let whid = await readField(wf, '#edit\\:whid');
  let whna = await readField(wf, '#edit\\:whna');
  // 选仓后 whid 仍空：可能弹窗没弹出或点错，重试一次触发器+选中文本
  if (wanted && !whid){
    steps.push(`whid 为空，重试选仓...`);
    for (const s of triggerSels){
      try{ if (await wf.locator(s).count() > 0){ await wf.locator(s).first().click({ timeout: 5000 }); triggered = s; await page.waitForTimeout(800); break; } }catch{}
    }
    // 重试也先试 waho 表格行
    for (const f of page.frames()){
      try{
        if (!/waho\.jsf/.test(f.url())) continue;
        const rows = f.locator('tr').filter({ hasText: wanted });
        if (await rows.count() > 0){
          await rows.first().click({ timeout: 5000 });
          chosen = true; how = (how ? how+'+' : '') + 'retry-waho-row';
          await page.waitForTimeout(800); break;
        }
      }catch{}
    }
    const retrySources = [];
    try{ retrySources.push({ loc: page.getByText(wanted, { exact: true }), where: 'main' }); }catch{}
    for (const f of page.frames()){ try{ retrySources.push({ loc: f.getByText(wanted, { exact: true }), where: 'frame:' + (f.name() || f.url().slice(-24)) }); }catch{} }
    for (const p of popups){ try{ retrySources.push({ loc: p.getByText(wanted, { exact: true }), where: 'popup' }); }catch{} }
    for (const src of retrySources){
      try{ if (await src.loc.count() > 0){ await src.loc.first().click({ timeout: 5000 }); chosen = true; how = (how ? how+'+' : '') + 'retry-text@' + src.where; break; } }catch{}
    }
    await page.waitForTimeout(600);
    whid = await readField(wf, '#edit\\:whid');
    whna = await readField(wf, '#edit\\:whna');
  }
  return { wf, triggered, chosen, how, whid, whna, orid: await readField(wf, '#edit\\:orid') };
}
async function dumpCreateDiagnosis(wf, label, extra = {}){
  try{
    await fs.mkdir(diagDir, { recursive: true });
    const frames = page.frames().map(f => ({ name: f.name(), url: f.url() }));
    const fields = {
      whid: await readField(wf, '#edit\\:whid'), whna: await readField(wf, '#edit\\:whna'),
      orid: await readField(wf, '#edit\\:orid'), orna: await readField(wf, '#edit\\:orna'),
      biid: await readBiid(wf), msg: await readEditMsg(wf),
    };
    const counts = await buttonCounts(wf, SAVE_CANDIDATES.concat(['#edit\\:addHead', '#edit\\:biid', '#edit\\:whna', '#whid_img', '#edit\\:inButton']));
    const visibleCounts = await buttonVisibleCounts(wf, SAVE_CANDIDATES);
    const buttons = await wf.locator('input,button,a,select').evaluateAll(
      xs => xs.map(x => {
        const r = x.getBoundingClientRect(); const cs = getComputedStyle(x);
        return { tag: x.tagName, id: x.id, name: x.name, type: x.getAttribute('type'),
          value: (x.value || x.textContent || '').trim().slice(0, 80),
          onclick: (x.getAttribute('onclick') || '').slice(0, 220),
          visible: !!(x.offsetParent || r.width > 0), display: cs.display, disabled: !!x.disabled, w: Math.round(r.width), h: Math.round(r.height) };
      }).filter(x => x.id || x.value || x.onclick || x.name)
    );
    const warehouseCandidates = await wf.evaluate(() => {
      const list = [];
      document.querySelectorAll('input,select,a,img,span,td,div').forEach(x => {
        const id = x.id || '', name = x.name || '', t = (x.value || x.textContent || '').trim();
        if (/wh/i.test(id + name) || /仓库|货主|组织架构|验机中心|回收仓/.test(t)) {
          const r = x.getBoundingClientRect(); const cs = getComputedStyle(x);
          list.push({ tag: x.tagName, id, name, value: t.slice(0, 60), type: x.getAttribute('type'),
            visible: !!(x.offsetParent || r.width > 0), display: cs.display, disabled: !!x.disabled,
            options: x.tagName === 'SELECT' ? [...x.options].map(o => o.textContent.trim()) : undefined });
        }
      });
      return list.slice(0, 90);
    }).catch(() => []);
    const frameTexts = [];
    for (const f of page.frames()){ try{ const body = await f.evaluate(() => document.body ? document.body.innerText.slice(0, 600) : ''); if (/仓|中心/.test(body)) frameTexts.push({ url: f.url(), hasWanted: config.warehouse ? body.includes(config.warehouse) : false, text: body }); }catch{} }
    const globalMv = await globalMvSet();
    const payload = {
      label, at: new Date().toISOString(), pageUrl: page.url(), wfUrl: wf.url(),
      fields, counts, visibleCounts, frames, globalMv, listRows: await listOrderIds(wf).catch(() => []),
      lastDialogMessage, buttons, warehouseCandidates, frameTexts, ...extra,
    };
    await fs.writeFile(path.join(diagDir, 'create-order-fail.json'), JSON.stringify(payload, null, 2), 'utf8');
    await saveDiagnostic(page, diagDir, 'create-order-fail');
  }catch{ /* 诊断落盘失败不覆盖原始异常 */ }
}
// 解析服务端导入反馈：区分“成功/失败/中性”，并提取声称导入行数；避免把“导入成功!总共500行”误当错误
function parseImportMessage(text){
  const t=String(text||'').replace(/\s+/g,' ').trim();
  // 导入成功关键词：只认真正的导入成功，不认"单据添加成功"（那是建单成功提示）
  const success=/导入成功|成功导入|数据被导入|导入完成|已成功导入|导入明细成功/.test(t);
  const fail=/导入失败|导入错误|拒绝|不能为空|校验失败|不存在|未找到|无效条码|导入异常|错误：|:错误/.test(t);
  const rowsMatch=t.match(/(?:总共|共|成功导入|导入|添加)?\s*(\d+)\s*(?:行|条)(?:数据被导入|条|行)?/);
  const rows=rowsMatch?Number(rowsMatch[1]):null;
  return { success, fail, rows, text:t };
}
// 轮询明细条数；A4J 局部提交不整页刷新，先在当前帧计数，超时再重开单据读最终值（不重复提交）
async function waitRecordCount(wf,orderId,expected,timeoutMs){
  const t0=Date.now(); let last=await orderRecordCount(wf).catch(()=>0);
  while(Date.now()-t0<timeoutMs){
    const cur=await orderRecordCount(wf).catch(()=>last);
    if(Number.isFinite(cur)) last=cur;
    if(cur>=expected) return cur;
    await page.waitForTimeout(500);
  }
  try{ const w=await openOrder(orderId); last=await orderRecordCount(w); }catch{}
  return last;
}
async function importOrder(chunk,orderId){
  let wf=await openOrder(orderId); lastDialogMessage='';
  const before=await orderRecordCount(wf);
  if(before===Number(chunk.expected)){await log(`${chunk.name} ${orderId} 已有 ${before} 条，确认已完成并跳过上传`); return {name:chunk.name,category:chunk.category,expected:chunk.expected,orderId,status:'success'};}
  if(before!==0){
    // 半成品残留（0<before<expected）：不向非空单叠加导入，交由循环自动新建干净单；满量已由上方幂等跳过
    const err=new Error(`${chunk.name} ${orderId} 当前已有 ${before} 条，期望为 0 或 ${chunk.expected}（疑似半成品残留，不叠加导入，将自动新建干净单）`);
    err.needsRecreate=true; err.staleCount=before; err.staleOrderId=orderId;
    throw err;
  }
  // 打开"选择导入文件"弹窗并选择文件
  await wf.locator('#edit\\:inButton').click();
  await page.waitForTimeout(500);
  const file=wf.locator('#file\\:upFile'); await file.waitFor({state:'attached',timeout:15000});
  await file.setInputFiles(chunk.xlsPath);
  await log(`${chunk.name} 已选择导入文件 ${chunk.xlsPath}`);
  const expect=Number(chunk.expected); let method='';
  // 通道A：可见“上传”按钮 #file:import（onclick=doImport + A4J.AJAX.Submit 局部提交，符合页面真实交互）
  const importBtn=wf.locator('#file\\:import');
  if(await importBtn.count().catch(()=>0)>0){
    await importBtn.first().click({timeout:15000}); method='click:#file:import';
    await log(`${chunk.name} 已点击“上传”按钮 #file:import`);
  }else{
    await log(`${chunk.name} 未发现可见 #file:import，转入提交/fetch 兜底`);
  }
  let after=await waitRecordCount(wf,orderId,expect,18000);
  // 服务端成功提示旁证：必须服务端明确说导入了 expect 行才算成功，不认"添加成功"
  let mes=await readField(wf,'#mes').catch(()=>'');
  let msg=await readEditMsg(wf);
  const pm0=parseImportMessage([mes,msg,lastDialogMessage].filter(Boolean).join('；'));
  let okByMsg=pm0.success && pm0.rows===expect;
  if(okByMsg && after!==expect) await log(`${chunk.name} 服务端成功提示「${pm0.text.slice(0,120)}」，按服务端行数确认（records=${after}，服务端=${pm0.rows}），不再重复提交`);
  // 通道B：A4J 未生效且无成功确认时，用 JS 触发隐藏 submit（#file:importBut）或可见按钮 click
  if(after!==expect && !okByMsg){
    method+='|submit-js';
    await wf.evaluate(()=>{ const b=document.getElementById('file:importBut')||document.getElementById('file:import'); if(b) b.click(); }).catch(()=>{});
    await log(`${chunk.name} 点击后条数 ${after}/${expect}，JS 触发提交兜底`);
    after=await waitRecordCount(wf,orderId,expect,12000);
    mes=await readField(wf,'#mes').catch(()=>mes); msg=await readEditMsg(wf);
    const pmB=parseImportMessage([mes,msg,lastDialogMessage].filter(Boolean).join('；'));
    if(pmB.success && pmB.rows===expect) okByMsg=true;
  }
  // 通道C：fetch multipart 最后兜底；成功/失败消息分类识别，不再把"导入成功"当错误
  if(after!==expect && !okByMsg){
    method+='|fetch';
    try{
      const result=await wf.evaluate(async()=>{
        const form=document.querySelector('form#file');
        if(!form) return {ok:false,status:0,text:''};
        const data=new FormData(form); data.set('file:import','file:import'); data.set('file:importBut','上传');
        const response=await fetch(form.action,{method:'POST',body:data,credentials:'include',redirect:'follow'});
        return {ok:response.ok,status:response.status,text:await response.text()};
      });
      if(result.text) await fs.writeFile(path.join(outDir,`后台上传响应_${chunk.name}.html`),result.text,'utf8');
      let fetchMs='';
      const m=result.text.match(/var\s+ms\s*=\s*"([\s\S]*?)"\s*;/i);
      if(m){ fetchMs=m[1].replace(/<br\s*\/?\s*>/gi,'\n').replace(/\\"/g,'"').split('\n').map(x=>x.trim()).filter(Boolean).join('；'); }
      const pmC=parseImportMessage(`${fetchMs}；${mes}；${msg}`);
      if(pmC.success){ await log(`${chunk.name} fetch 后端成功提示: ${pmC.text.slice(0,150)}`); if(pmC.rows===expect) okByMsg=true; }
      else if(pmC.fail){ await log(`${chunk.name} fetch 后端拒绝提示: ${pmC.text.slice(0,150)}`); }
      after=await waitRecordCount(wf,orderId,expect,12000);
    }catch(e){ await log(`${chunk.name} fetch 兜底异常: ${e.message}`); }
  }
  const finalMes=await readField(wf,'#mes').catch(()=>mes);
  const finalMsg=await readEditMsg(wf);
  const pmF=parseImportMessage([finalMes,finalMsg,lastDialogMessage].filter(Boolean).join('；'));
  // 导入成功必须严格校验行数匹配：服务端说成功但没说行数时，必须 after === expect
  const ok = after===expect || (pmF.success && pmF.rows===expect);
  if(!ok){
    let fileSize=0, fileErr=''; try{fileSize=fs.statSync(chunk.xlsPath).size;}catch(e){fileErr=e.message;}
    // 业务拒绝：同库位冲突 / 可用库存不足（这批货已被移走）= 正常业务跳过，不报错中止
    const alreadyAtTarget = /同一库位|不能为同一库位|已存在|重复建|重复导入|可用库存不足|库存不足|当前库位的可用库存/.test(pmF.text);
    const fail={chunk:chunk.name,orderId,expected:expect,actual:after,method,backendMessage:pmF.text,backendRows:pmF.rows,alreadyAtTarget,mes:finalMes,msg:finalMsg,lastDialogMessage,file:chunk.xlsPath,fileSize,fileErr,url:wf.url()};
    try{await fs.mkdir(diagDir,{recursive:true}); await fs.writeFile(path.join(diagDir,'import-fail.json'),JSON.stringify(fail,null,2),'utf8');}catch{}
    if(alreadyAtTarget){
      await log(`${chunk.name} ${orderId} 同库位冲突：这批货已在目标库位/此前已成功建单，标记 blocked 跳过，继续下一块。请在 WMS 列表找到该块真正成功的移库单，把 orders.json 里它改成 {"status":"success","orderId":"真实成功单号"}。`);
      return {name:chunk.name,category:chunk.category,expected:chunk.expected,orderId,status:'blocked',reason:'alreadyAtTarget'};
    }
    throw new Error(`${chunk.name} ${orderId} 上传后明细 ${after} 条，期望 ${expect}（提交方式=${method}；服务端提示=${pmF.text.slice(0,160)||'无'}；文件 ${fileSize} 字节）；详见 diagnosis/import-fail.json 与 后台上传响应_${chunk.name}.html`);
  }
  const confirmedRows=after===expect?expect:(pmF.rows??after);
  await log(`${chunk.name} ${orderId} 导入确认完成 ${confirmedRows}（${method}${okByMsg||pmF.success?';服务端成功提示确认':''}）`);
  return {name:chunk.name,category:chunk.category,expected:chunk.expected,orderId,status:'success'};
}
async function createAndImport(chunk,onCreated){
  const steps=[];
  let wf=await currentWorkFrame();
  const beforeIds=await globalMvSet();
  // 1) 列表页点“添加单据”：#edit:addHead 优先，兼容 input[value=添加单据]
  const addSel=await clickFirstVisible(wf,['#edit\\:addHead','input[value="添加单据"]']);
  const listProbe=await probeAllCounts();
  steps.push(`addSel=${addSel||'null'}; beforeIds=${beforeIds.length}; listProbe=${JSON.stringify(listProbe)}`);
  await log(`${chunk.name} 添加单据命中: ${addSel||'未找到可见添加单据按钮'}`);
  // 第一次没找到：刷新列表页再试一次
  if(!addSel){
    await log(`${chunk.name} 第一次未找到添加单据按钮，刷新列表页重试`);
    try{ await wf.reload({waitUntil:'domcontentloaded'}); await wf.waitForTimeout(2500); }catch{}
    addSel = await clickFirstVisible(wf, ['#edit\\:addHead', 'input[value="添加单据"]']);
    await log(`${chunk.name} 刷新后添加单据命中: ${addSel||'仍未找到'}`);
  }
  if(!addSel){
    const probe=await probeAllCounts();
    await dumpCreateDiagnosis(wf,'未找到添加单据按钮',{chunk,steps,beforeIds,probe,lastDialog:lastDialogMessage});
    throw new Error(`${chunk.name} 未找到可见"添加单据"按钮（当前 ${wf.url()}）。可能不在列表页，请手动刷新浏览器后重跑。详见 diagnosis/create-order-fail.json`);
  }
  // 2) 等待编辑表单出现（同帧刷新 / 新子 frame / 弹窗均可）
  const ec=await waitEditContext(15000);
  // 检测 500 错误页（精确匹配：错误提示文本，不是数字里的500）
  if(ec){
    const bodyText = await ec.obj.locator('body').allTextContents().catch(()=>[]);
    const bt = bodyText.join('');
    if (bt.includes('哎哟喂') || bt.includes('页面出问题') || bt.includes('Internal Server Error')){
      await dumpCreateDiagnosis(wf,'服务端500错误页',{chunk,steps,beforeIds,url:ec.url});
      throw new Error(`${chunk.name} 进入编辑页时服务端返回错误（旧单可能损坏）。已自动重置到列表页，请刷新浏览器后重跑。详见 diagnosis/create-order-fail.json`);
    }
  }
  if(!ec){
    const probe=await probeAllCounts();
    await dumpCreateDiagnosis(wf,'添加单据后未识别到新建/编辑表单',{chunk,steps,beforeIds,probe,lastDialog:lastDialogMessage});
    throw new Error(`${chunk.name} 点击“添加单据”后未识别到 move_add/move_edit 表单（当前 ${wf.url()}；各 frame 保存候选计数见 probe；系统弹窗: ${lastDialogMessage||'无'}）；详见 diagnosis/create-order-fail.json，已安全中止防错单`);
  }
  wf=ec.obj; steps.push(`editContext=${ec.kind}; url=${ec.url}`);
  await log(`${chunk.name} 已进入编辑表单(${ec.kind}) ${ec.url}`);
  // 点"添加单据"应该进入新建页 move_add；如果进入的是 move_edit（带 pid），自动刷新回列表页重试一次
  if (/move_edit\.jsf.*pid=MV/.test(ec.url || wf.url())){
    await log(`${chunk.name} 误入编辑页 ${ec.url}，自动刷新回列表页重试`);
    try{ await wf.goto(new URL('move.jsf', wf.url()).toString(), {waitUntil:'domcontentloaded'}); await wf.waitForTimeout(2000); }catch{}
    // 重新点添加单据
    const addSel2 = await clickFirstVisible(wf, ['#edit\\:addHead', 'input[value="添加单据"]']);
    steps.push(`重试添加单据命中: ${addSel2||'null'}`);
    if(!addSel2){ throw new Error(`${chunk.name} 重试添加单据后仍未找到按钮，请刷新浏览器后重跑。详见 diagnosis/create-order-fail.json`); }
    const ec2 = await waitEditContext(15000);
    if(!ec2 || /move_edit\.jsf.*pid=MV/.test(ec2.url || '')){
      throw new Error(`${chunk.name} 重试后仍进入编辑页而非新建页（${ec2?.url||'未知'}）。请手动刷新浏览器后重跑。`);
    }
    wf = ec2.obj; steps.push(`重试后编辑表单=${ec2.kind}; url=${ec2.url}`);
    await log(`${chunk.name} 重试后进入编辑表单(${ec2.kind}) ${ec2.url}`);
  }
  // 3) 新建页必须选仓库
  const wh=await selectWarehouse(wf,steps); wf=wh.wf;
  await log(`${chunk.name} 仓库 whid=${wh.whid||'(空)'} whna=${wh.whna||'(空)'} 触发器=${wh.triggered||'无'} 选中=${wh.chosen}(${wh.how||'无'})`);
  if(!wh.whid){
    const probe=await probeAllCounts();
    await dumpCreateDiagnosis(wf,'新建页选仓失败 whid 为空',{chunk,steps,wh,probe,lastDialog:lastDialogMessage});
    throw new Error(`${chunk.name} 新建页选仓失败：仓库 whid 为空（触发器=${wh.triggered||'未命中'}、选中=${wh.chosen}、后端提示=${(await readEditMsg(wf))||'无'}）。未点保存避免无效请求。详见 diagnosis/create-order-fail.json。`);
  }
  // 4) 保存单据：新建页 move_add 常用 #edit:addId，编辑页 move_edit 为 #edit:updateId，统一候选逐个尝试；存在但隐藏时降级 JS 点击
  const saveCounts=await buttonCounts(wf,SAVE_CANDIDATES);
  const saveVisible=await buttonVisibleCounts(wf,SAVE_CANDIDATES);
  const saveSel=await clickFirstVisible(wf,SAVE_CANDIDATES);
  steps.push(`saveSel=${saveSel||'null'}; saveCounts=${JSON.stringify(saveCounts)}; saveVisible=${JSON.stringify(saveVisible)}; whid=${wh.whid||''}; whna=${wh.whna||''}; chosen=${wh.chosen}; how=${wh.how||''}`);
  await log(`${chunk.name} 保存单据命中: ${saveSel||'未找到保存单据按钮'}；存在计数=${JSON.stringify(saveCounts)}；可见计数=${JSON.stringify(saveVisible)}`);
  if(!saveSel){
    const probe=await probeAllCounts();
    await dumpCreateDiagnosis(wf,'编辑表单内未找到保存按钮',{chunk,steps,beforeIds,wh,probe,lastDialog:lastDialogMessage});
    throw new Error(`${chunk.name} 编辑表单内未点到保存单据按钮（whid=${wh.whid||'空'}；系统弹窗: ${lastDialogMessage||'无'}）；详见 diagnosis/create-order-fail.json，已安全中止`);
  }
  // 4) 取号：必须等 #edit:biid 从"自动生成"变成真实 MV 单号，不猜号
  let orderId='';
  for(let i=0;i<24;i++){
    const biidVal = await wf.locator('#edit\\:biid').inputValue().catch(()=>'');
    if (biidVal && biidVal !== '自动生成' && /^MV\d+$/.test(biidVal)){ orderId = biidVal; break; }
    const urlM=((wf.url().match(/pid=(MV\d+)/)||page.url().match(/pid=(MV\d+)/)||[])[1]||'');
    if (urlM){ orderId = urlM; break; }
    await page.waitForTimeout(500);
  }
  const msg=await readEditMsg(wf);
  if(!orderId||!/^MV\d+$/.test(orderId)){
    await dumpCreateDiagnosis(wf,'保存后未取得合法单号',{chunk,steps,beforeIds,wh,saveSel,msg});
    throw new Error(`${chunk.name} 保存后未取得合法 MV 单号（#edit:biid 仍为"自动生成"或空、保存按钮=${saveSel}、后端提示=${msg||'无'}）。说明保存可能未成功，已安全中止防错导。详见 diagnosis/create-order-fail.json`);
  }
  await log(`${chunk.name} 取得移库单号 ${orderId}${msg?('；后端提示: '+msg):''}`);
  await onCreated?.(orderId);
  return await importOrder(chunk,orderId);
}
async function createOrders(onlyFirst=false){
  await login(); await openMove(); const _chunksRaw=JSON.parse(await fs.readFile(path.join(outDir,'chunks.json'),'utf8')); const chunks=Array.isArray(_chunksRaw)?_chunksRaw:_chunksRaw.chunks;
  let orders=[]; try{orders=JSON.parse(await fs.readFile(path.join(outDir,'orders.json'),'utf8'))}catch{}
  for(const chunk of chunks){const doneOrder=orders.find(x=>x.name===chunk.name&&(x.status==='success'||x.status==='blocked')); if(doneOrder){await log(`${chunk.name} 已${doneOrder.status==='success'?'完成':'标记 blocked 待人工确认'}，跳过`); if(onlyFirst) break; continue;}
    const existing=orders.find(x=>x.name===chunk.name&&x.status==='created'&&/^MV\d+$/.test(x.orderId));
    const checkpoint=async orderId=>{orders=orders.filter(x=>x.name!==chunk.name); orders.push({name:chunk.name,category:chunk.category,expected:chunk.expected,orderId,status:'created'}); await fs.writeFile(path.join(outDir,'orders.json'),JSON.stringify(orders,null,2),'utf8');};
    let result;
    try{
      result = existing ? await importOrder(chunk,existing.orderId) : await createAndImport(chunk,checkpoint);
    }catch(e){
      if(e.needsRecreate || e.needsReopen){
        const reason = e.needsReopen ? `旧单 ${e.staleOrderId||''} 打不开编辑页` : `单据 ${e.staleOrderId||''} 已有 ${e.staleCount} 条半成品`;
        await log(`${chunk.name} ${reason}，不叠加导入，清除断点并自动新建干净单（旧单保留待人工清理）`);
        orders=orders.filter(x=>x.name!==chunk.name);
        await fs.writeFile(path.join(outDir,'orders.json'),JSON.stringify(orders,null,2),'utf8');
        await openMove();
        result=await createAndImport(chunk,checkpoint);
      }else throw e;
    }
    orders=orders.filter(x=>x.name!==chunk.name); orders.push(result); await fs.writeFile(path.join(outDir,'orders.json'),JSON.stringify(orders,null,2),'utf8');
    const wf=await currentWorkFrame(); const back=wf.getByRole('button',{name:'返回'}); if(await back.count()) await back.click(); await page.waitForTimeout(500);
    if(onlyFirst) break;
  }
}
async function inspectOrders(){
  await login(); await openMove(); const wf=await currentWorkFrame();
  const bodies=await wf.locator('body').allTextContents({timeout:15000});
  const inputs=await wf.locator('input,select,button,a').evaluateAll(xs=>xs.map(x=>({tag:x.tagName,id:x.id,name:x.getAttribute('name'),value:x.value||x.textContent?.trim(),type:x.getAttribute('type'),display:getComputedStyle(x).display})).filter(x=>x.id||x.name||x.value));
  await fs.writeFile(path.join(outDir,'移库列表诊断.json'),JSON.stringify({bodies,inputs},null,2),'utf8');
  await log('移库列表诊断已保存');
}
async function inspectOrderDetail(){
  await login(); await openMove(); let wf=await currentWorkFrame();
  const linkInfo=await wf.locator('#gtable_biid_1').evaluate(el=>({outer:el.outerHTML,href:el.getAttribute('href'),onclick:el.getAttribute('onclick')})); await log(`首张移库单链接: ${JSON.stringify(linkInfo)}`);
  await wf.goto(new URL(linkInfo.href,wf.url()).toString(),{waitUntil:'domcontentloaded'}); await page.waitForTimeout(1000);
  const bodies=await wf.locator('body').allTextContents({timeout:15000});
  const inputs=await wf.locator('input,select,button,a').evaluateAll(xs=>xs.map(x=>({tag:x.tagName,id:x.id,name:x.getAttribute('name'),value:x.value||x.textContent?.trim(),type:x.getAttribute('type')})).filter(x=>x.id||x.name||x.value));
  await fs.writeFile(path.join(outDir,'移库详情诊断.json'),JSON.stringify({bodies,inputs},null,2),'utf8'); await log('移库详情诊断已保存');
}
async function inspectImport(){
  await login(); await openMove(); let wf=await currentWorkFrame(); await wf.goto(new URL('move_edit.jsf?pid=MV26082300002',wf.url()).toString(),{waitUntil:'domcontentloaded'});
  await wf.locator('#edit\\:inButton').click(); await page.waitForTimeout(700);
  const frames=page.frames().map(f=>({name:f.name(),url:f.url()})); const fields=[];
  for(const f of page.frames()){if(f.url().includes('/stocksys/move/')||f.url().includes('import')) fields.push(...await f.locator('input,button,a').evaluateAll(xs=>xs.map(x=>({tag:x.tagName,id:x.id,name:x.name,value:x.value||x.textContent?.trim(),type:x.type}))));}
  await fs.writeFile(path.join(outDir,'导入窗口诊断.json'),JSON.stringify({frames,fields},null,2),'utf8'); await log('导入窗口诊断已保存');
}
async function inspectImportBackend(){
  await login(); await openMove(); const wf=await openOrder('MV26082300002');
  await wf.locator('#edit\\:inButton').click(); await page.waitForTimeout(500);
  const data=await wf.evaluate(()=>({
    url:location.href,
    records:Number(document.querySelector('#gtable_records')?.value||0),
    forms:[...document.forms].map(f=>({id:f.id,name:f.name,action:f.action,method:f.method,enctype:f.enctype,outer:f.outerHTML.slice(0,12000)})),
    upload:[...document.querySelectorAll('#file\\:import,#file\\:importBut,#file\\:upFile')].map(x=>({id:x.id,type:x.type,name:x.name,value:x.value,onclick:x.getAttribute('onclick'),outer:x.outerHTML}))
  }));
  await fs.writeFile(path.join(outDir,'后台上传表单诊断.json'),JSON.stringify(data,null,2),'utf8'); await log('后台上传表单诊断已保存');
}
// 只读探查新建页 move_add：不保存、不导入，导出全部控件与仓库候选，供离线对齐真实保存/选仓选择器
async function inspectAdd(){
  await login(); await openMove();
  let wf=await currentWorkFrame();
  await clickFirstVisible(wf,['#edit\\:addHead','input[value="添加单据"]']);
  await waitEditContext(12000);
  const ec=await locateEditContext();
  if(!ec){ throw new Error('点“添加单据”后未进入 move_add/move_edit；请把控制台与当前页发回'); }
  wf=ec.obj; await page.waitForTimeout(1000);
  const widgets=await wf.locator('input,button,select,a,td,span').evaluateAll(xs=>xs.map(x=>{
    const r=x.getBoundingClientRect(); const cs=getComputedStyle(x);
    return {tag:x.tagName,id:x.id,name:x.name,type:x.getAttribute('type'),value:(x.value||x.textContent||'').trim().slice(0,80),
      onclick:(x.getAttribute('onclick')||'').slice(0,260),visible:!!(x.offsetParent||r.width>0),display:cs.display,disabled:!!x.disabled,
      options:x.tagName==='SELECT'?[...x.options].map(o=>o.textContent.trim()):undefined};
  }).filter(x=>x.id||x.name||x.value||x.onclick));
  const whProbe={triggered:null,candidates:[]};
  for(const s of ['#whid_img','#edit\\:whna','[onclick*=whid]','select[name*=whid]']){
    if(await wf.locator(s).count().catch(()=>0)>0){ whProbe.triggered=s; await wf.locator(s).first().click({timeout:4000}).catch(()=>{}); await page.waitForTimeout(900); break; }
  }
  for(const f of page.frames()){
    try{
      const items=await f.locator('a,td,span,label,input,select').evaluateAll(xs=>xs.map(x=>({tag:x.tagName,id:x.id,name:x.name,value:(x.value||x.textContent||'').trim().slice(0,60),onclick:(x.getAttribute('onclick')||'').slice(0,200)})).filter(x=>/仓|wh|回收|验机/i.test(JSON.stringify(x))));
      if(items.length) whProbe.candidates.push({frame:f.url(),items:items.slice(0,60)});
    }catch{}
  }
  for(const p of popups){ whProbe.candidates.push({popup:p.url(),items:await p.locator('a,td,span,label,input').evaluateAll(xs=>xs.map(x=>({tag:x.tagName,id:x.id,value:(x.value||x.textContent||'').trim().slice(0,60),onclick:(x.getAttribute('onclick')||'').slice(0,200)}))).catch(()=>[])}); }
  const forms=await wf.evaluate(()=>[...document.forms].map(f=>({id:f.id,name:f.name,action:f.action,method:f.method,enctype:f.enctype})));
  const data={label:'inspect-add 新建页只读诊断(未保存任何单据)',url:wf.url(),kind:ec.kind,forms,widgets,whProbe};
  await fs.mkdir(diagDir,{recursive:true});
  await fs.writeFile(path.join(diagDir,'add-page.json'),JSON.stringify(data,null,2),'utf8');
  const lines=[];
  lines.push('URL '+wf.url()+'  kind='+ec.kind); lines.push('FORMS '+JSON.stringify(forms));
  lines.push('--- visible buttons/inputs/selects/links (id|action|value) ---');
  for(const w of widgets){ if(w.visible && ['BUTTON','INPUT','SELECT','A'].includes(w.tag)) lines.push(`${w.tag} #${w.id||''} name=${w.name||''} type=${w.type||''} value=${w.value||''} onclick=${w.onclick||''}`); }
  lines.push('--- wh/仓库/货主/组织 widgets（含隐藏）---');
  for(const w of widgets){ if(/wh|仓|货主|组织|回收|验机/i.test(JSON.stringify(w))) lines.push(`${w.visible?'[vis]':'[hid]'} ${w.tag} #${w.id||''} name=${w.name||''} value=${w.value||''} options=${(w.options||[]).join('|')} onclick=${w.onclick||''}`); }
  lines.push('--- warehouse probe candidates ---');
  for(const c of whProbe.candidates){ lines.push('FRAME '+(c.frame||c.popup)); for(const it of c.items.slice(0,40)) lines.push('   '+JSON.stringify(it)); }
  await fs.writeFile(path.join(diagDir,'add-page.txt'),lines.join('\n'),'utf8');
  await log('新建页只读诊断已保存 diagnosis/add-page.json 与 add-page.txt（未保存任何单据）');
}
try{if(mode==='download-inventory') await downloadInventory(); else if(mode==='inspect-orders') await inspectOrders(); else if(mode==='inspect-order-detail') await inspectOrderDetail(); else if(mode==='inspect-import') await inspectImport(); else if(mode==='inspect-import-backend') await inspectImportBackend(); else if(mode==='inspect-add') await inspectAdd(); else if(mode==='resume-one') await createOrders(true); else await createOrders();}catch(e){await log(`ERROR ${e.stack||e}`); await page.screenshot({path:path.join(outDir,'失败现场.png'),fullPage:true}); await saveDiagnostic(page,diagDir,'system-fail'); throw e;}finally{await context.close();}
