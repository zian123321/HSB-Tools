# WMS 批量移库半自动工具

面向仓储管理系统（WMS）的批量移库辅助脚本：读取商品明细清单，与库存报表匹配当前库位，按品类切块生成导入表，可自动登录系统建单导入，并产出条码文档、Code128 一维码、扫码核对工具与按单号的单据表单。

> 仅用于你本人有权限操作的 WMS 环境，请遵守所在企业的系统使用与数据合规要求。脚本不内置任何账号、密码或内网地址，相关信息由本地 `config.json` 或环境变量提供。

## 工作流程

1. **环境预检** `check.mjs`：检查浏览器、Node/Python 依赖、模板与可选 HAR 文件。
2. **下载库存报表** `inventory-download.mjs`：登录系统并下载最新库存明细（可在配置中关闭，改用本地报表）。
3. **条码匹配切块** `prepare.py`：源清单与库存报表按条码匹配库位；剔除已在目标库位的货品，按品类分组、限量切块；输出去向统计与过滤明细。
4. **生成导入表** `convert-xls.py`：纯 Python（xlwt）生成系统要求的 `.xls` 导入文件，并生成一张覆盖全部货品的对照表（保留移库 / 未匹配 / 已在目标库位）。
5. **自动建单导入** `system.mjs`：逐切块新建移库单、选仓、上传导入表、按系统回执判定成功/跳过；断点状态写入 `orders.json`，重复运行不重复建单。
6. **生成文档** `build-docs.py`：品类合并 Word、Code128 一维码、离线扫码核对 HTML、按移库单号命名的单据表单。

## 目录结构

```
.
├── 一键运行.bat              # Windows 双击入口
├── run.ps1                   # 主编排脚本（调用以下各步骤）
├── check.mjs                 # 启动预检
├── shared.mjs                # 公共模块：配置/日志/浏览器探测/登录/重试/诊断
├── inventory-download.mjs    # 库存报表下载
├── prepare.py                # 条码匹配、分组切块、去向统计
├── convert-xls.py            # 导入表与全量对照表生成
├── ensure-template.mjs       # 可选：自动补缺导入模板
├── system.mjs                # 自动建单与明细导入
├── build-docs.py             # Word / 条码 / 扫码工具 / 单据表单生成
├── 扫码核对工具_模板.html      # 离线扫码核对工具模板（数据运行时内嵌）
├── config.example.json        # 配置模板（复制为 config.json 后填写）
├── package.json               # Node 依赖声明
├── requirements.txt           # Python 依赖声明
├── 使用说明.md
└── 新电脑配置文档.md
```

## 环境要求

- Windows 10/11 64 位
- Node.js 18+
- Python 3.9+
- 系统已安装 Edge 或 Chrome（Playwright 驱动）

## 安装

在项目目录打开终端：

```cmd
npm install
npx playwright install
pip install -r requirements.txt
```

## 配置

复制 `config.example.json` 为 `config.json`，按实际环境填写：

| 字段 | 说明 |
|---|---|
| `systemUrl` | WMS 主页面地址 |
| `browserExecutable` | 浏览器可执行文件路径（留空则自动探测 Edge/Chrome） |
| `account` / `password` | 登录账号密码（也可用环境变量 `GWMS_ACCOUNT` / `GWMS_PASSWORD`） |
| `sourceFile` | 源商品明细 Excel 路径（运行时弹窗选择，可留空） |
| `inventoryReport` | 库存报表本地路径（自动下载时可留空） |
| `warehouse` | 移库仓库名称，需与系统内名称一致 |
| `targetLocation` | 入库目标库位编码 |
| `barcodeType` | 条码类型编码 |
| `maxPerOrder` | 每份单据最大条数 |
| `headless` | 是否后端无头运行（不弹浏览器窗口） |
| `downloadInventoryAutomatically` | 是否每次自动下载最新库存报表 |
| `createOrdersAutomatically` | 是否自动建单导入（关闭则只产出表格与文档） |
| `harFile` | 可选，抓包 HAR 模板路径 |
| `templatePath` / `templateUrl` | 导入模板本地路径 / 下载地址（可选） |

## 运行

双击 `一键运行.bat`，或在项目目录执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File run.ps1
```

启动后在弹窗中选择本次商品明细 Excel。

## 产物

运行完成后位于 `输出文件/`：

- `条码文档/`：品类 Word、分块导入表、扫码核对工具 HTML、按单号命名的单据表单、全量对照表
- `barcodes/`：各移库单号 Code128 PNG 与单号对照
- `chunks.json`：切块数据（含被剔除/未匹配套明细）
- `orders.json`：建单结果与断点状态
- `automation.log`：运行日志

## 容错与断点

- 各步骤按产物存在性跳过；已成功或被业务拒绝的单据再次运行不重复建单。
- 文件被占用（已在 Word/Excel 打开）时自动加时间戳另存，不中断整体流程。
- 导入失败、选仓失败等场景会在 `输出文件/diagnosis/` 落截图与页面 HTML 便于排查。

## 说明

- 导入表与文档由本地数据生成，反映的是**计划移库清单**；系统实际落单明细以 WMS 单据为准。
- 未匹配到库位的货品会在对照表中标注为“未匹配”，需核对条码一致性或库存报表是否导出完整。
