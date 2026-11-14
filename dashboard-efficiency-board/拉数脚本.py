#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多业务系统 · 每人每小时产出看板 —— 数据拉取 + 看板生成（脱敏示例版）
==============================================================
本仓库为脱敏后的作品集示例：接口地址、仓库编号、接口名、字段名均为占位，
接入你自己的环境时，按 SYSTEMS 配置段里的注释逐项替换为真实值即可。

用法：
  1. 在目标业务系统页面打开 F12 → Network → Fetch/XHR，找到对应列表接口
  2. 右键该请求 → Copy → Copy as cURL，把登录凭证交给「更新凭证.py」写入 凭证.json
  3. 运行：python 拉数脚本.py          （拉真实数据并生成看板；Windows 用 python，Linux/Mac 用 python3）
     预览：python 拉数脚本.py --demo    （用示例数据生成看板，无需联网/配置）
  4. 打开同目录下的看板 HTML 即可查看（含三个业务标签页）

运行模式：首次运行全量拉取最近 DAYS 天写入各自缓存；
之后每次只拉当天新记录并与缓存合并去重（增量），适合定时任务定时跑做近实时更新。

注意：登录凭证就是你的系统登录身份，请勿外传；本脚本仅用于你本人获得授权的工作场景。
"""
import sys, os, json, datetime, time, random, re, shutil

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(BASE_DIR, "模板.html")
OUTPUT   = os.path.join(BASE_DIR, "产出看板.html")

# ===================== 可调参数：优先读 config.json，缺失则用默认值 =====================
DEFAULT_CONFIG = {
    "days": 7,
    "page_size": 100,
    "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
}
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
CRED_PATH   = os.path.join(BASE_DIR, "凭证.json")


def _load_json(path, default):
    if not os.path.exists(path):
        return dict(default)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        out = dict(default)
        if isinstance(data, dict):
            out.update({k: v for k, v in data.items() if v not in (None, "")})
        return out
    except Exception:
        return dict(default)


_cfg = _load_json(CONFIG_PATH, DEFAULT_CONFIG)
DAYS      = int(_cfg.get("days", 7))
PAGE_SIZE = int(_cfg.get("page_size", 100))
UA        = _cfg.get("ua", DEFAULT_CONFIG["ua"])

# ===================== 登录凭证：优先读 凭证.json；为空再回退源码常量 =====================
# 正常由「更新凭证.py」写入 凭证.json；下面的源码常量仅作兜底，勿手填长期凭证
_FALLBACK_UID, _FALLBACK_TOKEN = "", ""
_cred = _load_json(CRED_PATH, {})
LOGIN_USER_ID = str(_cred.get("login_user_id") or _FALLBACK_UID)
LOGIN_TOKEN   = str(_cred.get("login_token") or _FALLBACK_TOKEN)
# ========================================================================


class AuthExpired(Exception):
    """登录凭证失效（3001/401/403 类），不重试、不阻塞其他数据源。"""
    pass

# 三个业务数据源。以下 url / interface / depotId / list_field 均为脱敏占位，
# 接入真实环境时，对照浏览器 F12 里对应列表接口的请求与返回 JSON 逐项替换。
SYSTEMS = [
    {
        "key": "rec",
        "name": "收货记录",
        "url": "https://api.example-wms.com/wms/receivelog",
        "interface": "your-wms-receive-interface",
        "headers": {
            "Content-Type": "application/json",
            "Origin": "https://app.example-wms.com",
            "Referer": "https://app.example-wms.com/",
            "User-Agent": UA,
        },
        "params": {"depotId": "DEPOT_ID_1"},   # 替换为你有仓库权限的仓库编号
        "list_field": "yourReceiveListField",   # 返回 JSON 中记录列表所在字段名
        "cache": "数据缓存.json",
        "max_pages": 400,
        "source": "WMS 收货记录",
    },
    {
        "key": "unpack",
        "name": "验货拆包",
        "url": "https://api.example-xy.com/xywms/getcheckinparcels",
        "interface": "your-xy-delivery-interface",
        "headers": {
            "Content-Type": "application/json",
            "Origin": "https://app.example-xy.com",
            "Referer": "https://app.example-xy.com/",
            "User-Agent": UA,
        },
        "params": {"depotId": "DEPOT_ID_2", "logisticsNum": None},
        "list_field": "yourParcelListField",
        "cache": "验货缓存.json",
        "max_pages": 200,
        "range_fields": ["operateStartTime", "operateEndTime"],  # 该接口用时间范围筛选
        "source": "验货系统拆包/验收入库记录",
    },
    {
        "key": "photo",
        "name": "真机拍照",
        "url": "https://api.example-detect.com/api/autoPhotoList",
        "interface": "your-photo-interface",
        "headers": {
            "Content-Type": "application/json",
            "Origin": "https://app.example-detect.com",
            "Referer": "https://app.example-detect.com/",
            "User-Agent": UA,
        },
        # 部分系统凭证字段名不同（user_id / login_token），且时间可能用斜杠格式
        "auth": {"user_id": LOGIN_USER_ID, "userId": LOGIN_USER_ID,
                 "loginUserId": LOGIN_USER_ID, "login_token": LOGIN_TOKEN},
        "params": {"serialNumber": ""},
        "list_field": "list",
        "cache": "拍照缓存.json",
        "max_pages": 300,
        "range_fields": ["startTime", "endTime"],
        "range_fmt": "%Y/%m/%d %H:%M:%S",
        "time_field": "createTime",
        "source": "检测系统·真机拍照产出",
    },
]


def auth_hint():
    print("提示：登录凭证有效期由系统服务器决定，无法永久有效（也无法绕过系统鉴权）。")
    print("      失效后请运行：python 更新凭证.py（推荐先把 Copy as cURL 存为 curl.txt 再 python 更新凭证.py curl.txt），一条命令自动换证。")


def build_body(page_index, cfg, range_start=None, range_end=None):
    param = dict(
        cfg.get("auth", {"loginUserId": LOGIN_USER_ID, "loginToken": LOGIN_TOKEN}),
        pageIndex=str(page_index),
        pageSize=str(PAGE_SIZE),
        **cfg["params"]
    )
    if cfg.get("range_fields") and range_start and range_end:
        param[cfg["range_fields"][0]] = range_start
        param[cfg["range_fields"][1]] = range_end
    return {
        "_head": {
            "_version": "0.01",
            "_msgType": "request",
            "_timestamps": str(int(time.time())),
            "_interface": cfg["interface"],
            "_remark": "",
        },
        "_param": param,
    }


_SESSION = None


def _session():
    global _SESSION
    if _SESSION is None:
        import requests
        _SESSION = requests.Session()
    return _SESSION


def fetch_page(page_index, cfg, range_start=None, range_end=None):
    """单页请求：网络抖动/5xx 指数退避重试；凭证错误抛 AuthExpired；其余错误抛 RuntimeError。"""
    headers = {k: v for k, v in cfg["headers"].items() if v}
    last_err = None
    for attempt in range(3):
        try:
            r = _session().post(cfg["url"], headers=headers,
                                 json=build_body(page_index, cfg, range_start, range_end), timeout=30)
            if r.status_code in (401, 403):
                raise AuthExpired("HTTP %s 登录凭证失效" % r.status_code)
            if 500 <= r.status_code < 600:
                last_err = "HTTP %s" % r.status_code
                time.sleep(2 + 4 * attempt + random.uniform(0.5, 1.5))
                continue
            r.raise_for_status()
            j = r.json()
            d = j.get("_data") or {}
            ec = str(d.get("_errCode") or "")
            es = str(d.get("_errStr") or "")
            if ec not in ("0", "") or (es and es.lower() != "success"):
                if any(k in es for k in ("登录", "凭证", "token", "Token", "login", "Login")):
                    raise AuthExpired("%s %s" % (ec, es))
                raise RuntimeError("接口返回错误：" + json.dumps(d, ensure_ascii=False)[:300])
            return j
        except AuthExpired:
            raise
        except Exception as e:
            last_err = str(e)
            if attempt < 2:
                time.sleep(2 + 4 * attempt + random.uniform(0.5, 1.5))
    raise RuntimeError("重试 3 次仍失败：%s" % last_err)


def norm(rec, cfg):
    """把两个系统的原始记录统一成看板通用字段。"""
    if cfg["key"] == "rec":
        return {
            "id": str(rec.get("id")),
            "user": rec.get("addUser") or "",
            "time": (rec.get("addTime") or "").strip(),
            "num": int(rec.get("num") or rec.get("actReceiveNum") or 1),
            "status": rec.get("statusCn") or "",
            "channel": (rec.get("orderWeb") or "").strip(),
            "logistics": rec.get("logisticsNumber") or "",
            "depot": rec.get("depotId") or "",
        }
    if cfg["key"] == "photo":
        return {
            "id": str(rec.get("recordId")),
            "user": rec.get("realName") or "",
            "time": (rec.get("createTime") or "").strip(),
            "num": 1,
            "status": "已拍照",
            "channel": rec.get("serialNumber") or "",      # 条码
            "logistics": rec.get("deviceId") or "",         # 设备ID
            "depot": "检测系统（示例）",
        }
    return {
        "id": str(rec.get("id")),
        "user": rec.get("addUser") or "",
        "time": (rec.get("addTime") or "").strip(),
        "num": 1,
        "status": "已拆包",
        "channel": ("包裹" + str(rec.get("parcelId") or "")).strip() if rec.get("parcelId") else "",
        "logistics": rec.get("logisticsNum") or "",
        "depot": "验货系统（示例）",
    }


def pull_records(cfg):
    """拉取最近 DAYS 天记录（接口按 addTime 倒序返回，扫过截止线即停）。

    增量模式：若存在缓存文件，则只拉今天（>= 今天 0 点）的新记录，与缓存合并去重；
    首次运行（无缓存）则全量翻页拉最近 DAYS 天。
    """
    now = datetime.datetime.now()
    cache_path = os.path.join(BASE_DIR, cfg["cache"])
    cache = None
    if os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            cache = None
    if isinstance(cache, list) and cache:
        cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
        base = {}
        for r in cache:
            if isinstance(r, dict) and r.get("id"):
                base[str(r["id"])] = r
        print("增量模式：%s 缓存载入 %d 条，本次只拉当天新记录…" % (cfg["name"], len(base)))
    else:
        # 最近 DAYS 个自然日（含今天）：从 (今天-DAYS+1) 的 0 点起算
        cutoff = (now - datetime.timedelta(days=DAYS - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
        base = {}
        print("全量模式：%s 首次运行，翻页拉取最近 %d 天…" % (cfg["name"], DAYS))

    # 需要时间范围筛选的接口（如验货拆包/真机拍照）：把起止时间带进每次请求，翻页只翻窗口内数据
    range_start = range_end = None
    if cfg.get("range_fields"):
        range_fmt = cfg.get("range_fmt", "%Y-%m-%d %H:%M:%S")
        range_start = cutoff.strftime(range_fmt)
        range_end = now.strftime(range_fmt)

    time_field = cfg.get("time_field", "addTime")   # 部分接口的时间字段不同（如 createTime）
    page = 0
    stopped = False
    while page < cfg["max_pages"]:
        resp = fetch_page(page, cfg, range_start, range_end)
        lst = ((resp.get("_data") or {}).get("_data") or {}).get(cfg["list_field"]) or []
        if not lst:
            break
        for rec in lst:
            rid = str(rec.get("id") or rec.get("recordId") or "")
            tstr = (rec.get(time_field) or "").strip()
            try:
                t = datetime.datetime.strptime(tstr, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                t = None
            if t and t < cutoff:
                stopped = True          # 已越过截止线（列表按时间倒序）
                break
            if rid in base:
                continue
            base[rid] = norm(rec, cfg)
        if stopped:
            break
        page += 1
        if page % 50 == 0:
            print("  …%s 已翻 %d 页，累计 %d 条" % (cfg["name"], page, len(base)))
        time.sleep(random.uniform(0.5, 2.0))   # 每页之间随机间隔，避免固定机器节奏
    if not stopped and page >= cfg["max_pages"]:
        print("警告：%s 达到翻页上限（%d 页），数据可能不完整，可调大 max_pages。" % (cfg["name"], cfg["max_pages"]))

    # 只保留最近 DAYS 个自然日，按时间倒序输出
    window_cut = (now - datetime.timedelta(days=DAYS - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    keep = {}
    for rid, r in base.items():
        tstr = (r.get("time") or "").strip()
        try:
            t = datetime.datetime.strptime(tstr, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            t = None
        if t is None or t >= window_cut:
            keep[rid] = r
    records = list(keep.values())
    records.sort(key=lambda r: r.get("time", ""), reverse=True)
    if not records:
        raise RuntimeError("%s 没拉到数据。请检查 URL / 登录凭证 / 筛选参数。" % cfg["name"])
    return records


def load_cache(cfg):
    """读取旧缓存作为失败兜底（返回 list，无则 []）。"""
    cache_path = os.path.join(BASE_DIR, cfg["cache"])
    if not os.path.exists(cache_path):
        return []
    try:
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_cache_atomic(cfg, records):
    """原子写缓存：先写 .tmp 再 os.replace；写前备份旧文件到 backups/，保留最近 3 天。"""
    path = os.path.join(BASE_DIR, cfg["cache"])
    bdir = os.path.join(BASE_DIR, "backups")
    if os.path.exists(path):
        try:
            os.makedirs(bdir, exist_ok=True)
            day = datetime.datetime.now().strftime("%Y%m%d")
            shutil.copy2(path, os.path.join(bdir, cfg["cache"] + "." + day + ".bak"))
            cutoff = datetime.datetime.now() - datetime.timedelta(days=3)
            for fn in os.listdir(bdir):
                m = re.search(r"(\d{8})\.bak$", fn)
                if m:
                    try:
                        old = datetime.datetime.strptime(m.group(1), "%Y%m%d")
                        if old < cutoff:
                            os.remove(os.path.join(bdir, fn))
                    except Exception:
                        pass
        except Exception as e:
            print("警告：%s 缓存备份失败（%s）。" % (cfg["cache"], e))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False)
    os.replace(tmp, path)


def demo_records(people, status, channel_prefix):
    """示例数据：近 DAYS 天、指定操作人的模拟记录（仅用于预览看板样式）。"""
    import random
    random.seed(42)
    records, rid = [], 1
    now = datetime.datetime.now()
    for day_offset in range(DAYS):
        d = now - datetime.timedelta(days=day_offset)
        for p in people:
            for h in range(8, 20):
                base = 2
                if h in (9, 10, 11):
                    base = 4
                if h in (14, 15, 16, 17):
                    base = 3
                count = max(0, base + random.randint(-2, 2))
                for _ in range(count):
                    t = d.replace(hour=h, minute=random.randint(0, 59), second=random.randint(0, 59))
                    records.append({
                        "id": str(rid), "user": p,
                        "time": t.strftime("%Y-%m-%d %H:%M:%S"),
                        "num": 1, "status": status,
                        "channel": channel_prefix + str(random.randint(100000, 999999)),
                        "logistics": "SF" + "".join(str(random.randint(0, 9)) for _ in range(13)),
                        "depot": "示例",
                    })
                    rid += 1
    return records


def render(rec_records, rec_meta, unpack_records, unpack_meta, photo_records, photo_meta, status=None):
    payload = json.dumps({
        "records": rec_records, "meta": rec_meta,
        "unpackRecords": unpack_records, "unpackMeta": unpack_meta,
        "photoRecords": photo_records, "photoMeta": photo_meta,
        "status": status or {},
    }, ensure_ascii=False)
    payload = payload.replace("</", "<\\/")     # 防止与 </script> 冲突
    with open(TEMPLATE, encoding="utf-8") as f:
        html = f.read()
    if "__DATA_JSON__" not in html:
        print("错误：模板.html 中找不到数据占位符 __DATA_JSON__。")
        sys.exit(1)
    html = html.replace("__DATA_JSON__", payload)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(html)
    print("已生成看板：%s" % OUTPUT)
    for records, cfg in ((rec_records, SYSTEMS[0]), (unpack_records, SYSTEMS[1]), (photo_records, SYSTEMS[2])):
        users = sorted({r["user"] for r in records})
        days = sorted({r["time"][:10] for r in records})
        if days:
            print("%s：%d 条 | 操作人：%s | 覆盖 %s ~ %s" % (
                cfg["name"], len(records), "、".join(users) or "—", days[0], days[-1]))
        else:
            print("%s：0 条（暂无缓存且本轮未取到数据；换证/联网成功后自动填充，不影响程序退出）" % cfg["name"])
    if rec_meta.get("demo"):
        print("当前为示例数据。配置好接口后运行 python3 拉数脚本.py 即替换为真实数据。")


def main():
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if len(sys.argv) > 1 and sys.argv[1] == "--demo":
        rec_records = demo_records(["张三", "李四", "王五", "赵六"], "已收货", "")
        unpack_records = demo_records(["王五", "张三", "钱七", "孙八"], "已拆包", "包裹")
        photo_records = demo_records(["周九", "吴十", "郑十一"], "已拍照", "")
        rec_meta = {"demo": True, "days": DAYS, "generated_at": now_str, "source": SYSTEMS[0]["source"], "ok": True, "state": "demo"}
        unpack_meta = {"demo": True, "days": DAYS, "generated_at": now_str, "source": SYSTEMS[1]["source"], "ok": True, "state": "demo"}
        photo_meta = {"demo": True, "days": DAYS, "generated_at": now_str, "source": SYSTEMS[2]["source"], "ok": True, "state": "demo"}
        status = {"generated_at": now_str, "auth_state": "demo", "sources": [
            {"name": SYSTEMS[0]["name"], "ok": True, "state": "demo", "count": len(rec_records), "message": ""},
            {"name": SYSTEMS[1]["name"], "ok": True, "state": "demo", "count": len(unpack_records), "message": ""},
            {"name": SYSTEMS[2]["name"], "ok": True, "state": "demo", "count": len(photo_records), "message": ""},
        ]}
    else:
        def run_source(cfg):
            """单源独立执行：成功原子存缓存；凭证失效/网络失败都保留旧缓存并返回，不阻断其他源。"""
            try:
                records = pull_records(cfg)
                save_cache_atomic(cfg, records)
                print("已更新缓存：%s（%d 条）" % (cfg["cache"], len(records)))
                return records, True, "ok", ""
            except AuthExpired as e:
                print("凭证失效：%s（%s）——保留旧缓存，继续其他数据源。" % (cfg["name"], e))
                auth_hint()
                return load_cache(cfg), False, "expired", str(e)
            except Exception as e:
                print("警告：%s 拉取失败（%s）——保留旧缓存，继续其他数据源。" % (cfg["name"], e))
                return load_cache(cfg), False, "error", str(e)

        rec_records, rec_ok, rec_state, rec_msg = run_source(SYSTEMS[0])
        unpack_records, up_ok, up_state, up_msg = run_source(SYSTEMS[1])
        photo_records, ph_ok, ph_state, ph_msg = run_source(SYSTEMS[2])

        states = (rec_state, up_state, ph_state)
        auth_state = "expired" if "expired" in states else ("error" if "error" in states else "ok")
        status = {
            "generated_at": now_str,
            "auth_state": auth_state,
            "sources": [
                {"name": SYSTEMS[0]["name"], "ok": rec_ok, "state": rec_state, "count": len(rec_records), "message": rec_msg},
                {"name": SYSTEMS[1]["name"], "ok": up_ok, "state": up_state, "count": len(unpack_records), "message": up_msg},
                {"name": SYSTEMS[2]["name"], "ok": ph_ok, "state": ph_state, "count": len(photo_records), "message": ph_msg},
            ],
        }
        rec_meta = {"demo": False, "days": DAYS, "generated_at": now_str, "source": SYSTEMS[0]["source"], "ok": rec_ok, "state": rec_state}
        unpack_meta = {"demo": False, "days": DAYS, "generated_at": now_str, "source": SYSTEMS[1]["source"], "ok": up_ok, "state": up_state}
        photo_meta = {"demo": False, "days": DAYS, "generated_at": now_str, "source": SYSTEMS[2]["source"], "ok": ph_ok, "state": ph_state}
    render(rec_records, rec_meta, unpack_records, unpack_meta, photo_records, photo_meta, status)
    # 把每源成败落盘，供常驻服务逐源打印与排障（不影响退出码：有缓存兜底时仍为 0）
    try:
        result_path = os.path.join(BASE_DIR, "last_result.json")
        with open(result_path, "w", encoding="utf-8") as f:
            json.dump({"generated_at": now_str, "status": status}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


if __name__ == "__main__":
    main()
