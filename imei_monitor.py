# -*- coding: utf-8 -*-
"""
检测拦截差异监控脚本
功能：
1. 轮询时间窗内已拦截记录（有条码 + IMEI 即纳入处理，不限制条码格式）
2. 同一 IMEI 下找另一条历史检测单做对照（优先同前缀，无同前缀再跨段取最新）
3. 仅比对前端展示的现场检测项（黑名单过滤）
4. 钉钉差异告警，按条码段规则映射负责人标识并署名：
   - 020 开头 / 字母+011 开头  → OWNER_A
   - 纯数字 010 / 字母+010     → OWNER_B
   - 其余段                   → 不署名

凭证、接口地址、机器人 webhook、负责人手机号均放在本地 config.py
（或环境变量）中填写，本文件不提交任何敏感信息。
"""
import os
import requests
import time
import re
import json
import traceback
from datetime import datetime, timedelta

# ===================== 配置加载（本地 config.py 优先，其次环境变量） =====================
try:
    import config as CFG
except ImportError:
    CFG = None


def _setting(key, default=""):
    """优先读本地 config.py，其次环境变量，都没有则用默认占位值。"""
    if CFG is not None and hasattr(CFG, key):
        return getattr(CFG, key)
    return os.environ.get(key, default)


def _setting_bool(key, default=False):
    return str(_setting(key, str(default))).strip().lower() in ("1", "true", "yes", "on")


# ---- 登录凭证（勿提交到仓库） ----
USERNAME = _setting("USERNAME", "")
PASSWORD = _setting("PASSWORD", "")

# ---- 业务接口地址（按实际系统在 config.py 中替换） ----
LOGIN_URL = _setting("LOGIN_URL", "https://api.example.com/login")
IMEI_LOCK_URL = _setting("IMEI_LOCK_URL", "https://api.example.com/imeiLockList")
DEVICE_RECORDS_URL = _setting("DEVICE_RECORDS_URL", "https://api.example.com/deviceRecords")
DETECT_DETAIL_URL = _setting("DETECT_DETAIL_URL", "https://api.example.com/getDetectRecordDetail")
# 待接入：自动通过接口（Payload 抓到后补齐 auto_pass_record 函数）
IMEI_LOCK_UPDATE_URL = _setting("IMEI_LOCK_UPDATE_URL", "https://api.example.com/imeiLockUpdate")
JUMP_URL = _setting("JUMP_URL", "http%3A%2F%2Fexample.com%2F")

# ---- 钉钉机器人 ----
DINGTALK_WEBHOOK = _setting("DINGTALK_WEBHOOK", "")
DINGTALK_KEYWORD = _setting("DINGTALK_KEYWORD", "【检测监控】")
DINGTALK_ENABLED = _setting_bool("DINGTALK_ENABLED", False)  # True=真实推送；False=仅控制台预览
SEND_STATUS_NOTIFY = _setting_bool("SEND_STATUS_NOTIFY", True)
SEND_STARTUP_NOTIFY = _setting_bool("SEND_STARTUP_NOTIFY", False)

# 负责人标识 -> 钉钉绑定手机号（真实 @ 需本人在群内且绑定该号；在 config.py 配置）
OWNER_MOBILES = getattr(CFG, "OWNER_MOBILES", {}) if CFG is not None else {}
if not isinstance(OWNER_MOBILES, dict):
    OWNER_MOBILES = {}

# ---- 播报时段与运行参数（可在 config.py 覆盖） ----
BROADCAST_START_HOUR = int(_setting("BROADCAST_START_HOUR", "10"))
BROADCAST_END_HOUR = int(_setting("BROADCAST_END_HOUR", "20"))
POLL_INTERVAL = int(_setting("POLL_INTERVAL", "60"))
ALERT_COOLDOWN = int(_setting("ALERT_COOLDOWN", "3600"))  # 同一设备重复告警冷却（秒）
DEBUG_MODE = _setting_bool("DEBUG_MODE", True)

# ---- 业务容错参数 ----
LOCK_PAGE_SIZE = 100
RECORD_LOOKBACK_DAYS = 30
MAX_HISTORY_PAGES = 20
FAIL_ALERT_THRESHOLD = 3

JSON_HEADERS = {"Content-Type": "application/json;charset=utf-8"}

auth_info = {"token": "", "user_id": "", "need_relogin": False}
processed_imei = {}

# ===================== 钉钉推送 =====================
def in_broadcast_window(now=None):
    """是否处于播报时段（10:00 <= 当前时间 < 20:00）。"""
    now = now or datetime.now()
    return BROADCAST_START_HOUR <= now.hour < BROADCAST_END_HOUR


def owner_to_mobile(owner_key):
    """负责人标识（OWNER_A / OWNER_B）-> 钉钉手机号，无映射返回空串。"""
    return OWNER_MOBILES.get(owner_key or "", "")


def send_dingtalk(content, at_mobiles=None):
    mobiles = [m for m in (at_mobiles or []) if m]
    # 钉钉官方要求：text 真@必须在正文 content 内出现 "@手机号"，再配合 at.atMobiles；
    # 仅传 atMobiles 而正文只写 "@姓名" 不会产生真@（文字@姓名属于假@，不触发通知/不高亮）
    at_text = " ".join(f"@{m}" for m in mobiles)
    body = f"{content}\n{at_text}" if mobiles else content
    full_content = f"{DINGTALK_KEYWORD}\n{body}"
    # 总开关：关闭时不发网络请求，仅把消息打印到控制台便于调试
    if not DINGTALK_ENABLED:
        at_note = f"\n[真实@手机号：{mobiles}]" if mobiles else ""
        print("\n[钉钉推送已关闭·控制台预览]\n" + full_content + at_note + "\n" + "-" * 50)
        return True
    try:
        payload = {"msgtype": "text", "text": {"content": full_content}}
        if mobiles:
            payload["at"] = {"atMobiles": mobiles, "isAtAll": False}
        resp = requests.post(
            DINGTALK_WEBHOOK,
            json=payload,
            timeout=5
        )
        result = resp.json()
        if result.get("errcode") != 0:
            print(f"[ERROR] 钉钉发送失败：{result.get('errmsg')}")
            return False
        if mobiles:
            print(f"[INFO] 已随消息@手机号 {mobiles}；若对方未收到提醒，确认该号为其钉钉账号本人绑定号且在群内")
        return True
    except Exception as e:
        print(f"[ERROR] 钉钉发送异常：{e}")
        return False

def send_error_alert(title, detail):
    if not SEND_STATUS_NOTIFY:
        return
    # 非播报时段静默，错误告警仅留控制台，不打扰群内
    if not in_broadcast_window():
        print(f"[INFO] 非播报时段，错误告警暂不推送：{title}")
        return
    msg = f"""异常类型：{title}
时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
详情：
{detail}"""
    send_dingtalk(msg)

# ===================== 工具函数 =====================
def clean_expired_records():
    now = time.time()
    expired = [k for k, v in processed_imei.items() if now - v > ALERT_COOLDOWN]
    for k in expired:
        del processed_imei[k]

def get_field(record, *keys):
    if not record:
        return ""
    record_lower = {k.lower(): v for k, v in record.items()}
    for key in keys:
        val = record_lower.get(key.lower())
        if val is not None and str(val).strip():
            return str(val).strip()
    return ""

def debug_print(msg):
    if DEBUG_MODE:
        print(msg)

# ===================== 登录鉴权 =====================
def login():
    for retry in range(3):
        try:
            payload = {
                "system_id": _setting("SYSTEM_ID", "48"),
                "jump_url": JUMP_URL,
                "username": USERNAME,
                "password": PASSWORD
            }
            resp = requests.post(LOGIN_URL, json=payload, headers=JSON_HEADERS, timeout=30)
            res = resp.json()
            body = res.get("body", {})

            if body.get("ret") == "0":
                data = body.get("data", {})
                auth_info["token"] = data.get("login_token", data.get("token", ""))
                auth_info["user_id"] = str(data.get("userId", data.get("user_id", "")))
                auth_info["need_relogin"] = False
                print("[OK] 登录成功，获取token")
                return True

            print(f"[WARN] 登录第{retry+1}次失败：{body.get('retinfo', '未知错误')}")
        except Exception as e:
            print(f"[WARN] 登录第{retry+1}次异常：{str(e)[:50]}")

        if retry < 2:
            time.sleep(2)

    send_error_alert("登录失败", "连续3次登录失败，请检查账号/网络")
    print("[ERROR] 连续多次登录失败")
    return False

def is_auth_failed(res):
    """判断 _data 结构响应是否为登录态失效（token过期/未登录/无权限）"""
    try:
        inner = res.get("_data", {})
        if str(inner.get("_ret", "0")) == "0":
            return False
        err_code = str(inner.get("_errCode", ""))
        err_str = str(inner.get("_errStr", "")).lower()
        keywords = ["token", "登录", "未登录", "失效", "invalid", "unauthorized", "权限", "login"]
        if any(k in err_str for k in keywords) or err_code in ("-1", "401", "403"):
            return True
        return False
    except Exception:
        return False

# ===================== 拦截记录接口（已取消条码格式限制，循环翻页） =====================
def get_intercept_list():
    """
    翻页拉取当天拦截记录，直到取完。
    返回 (records, failed)：records=记录列表；failed=True 表示本轮接口硬失败（网络异常/返回非0），
    供主循环做连续失败计数告警；成功但当天0条时 failed=False。
    """
    # 拦截列表窗口：前天 20:00 ~ 今天 23:59:59。
    # 覆盖周末/停机期间积累的拦截单，避免跨天漏报。
    today = datetime.now()
    start_dt = today - timedelta(days=2)
    lock_start = f"{start_dt.strftime('%Y-%m-%d')} 20:00:00"
    lock_end = f"{today.strftime('%Y-%m-%d')} 23:59:59"
    all_list = []
    failed = False
    page = 0

    while True:
        payload = {
            "_head": {"_interface": "imeiLockList"},
            "_param": {
                "loginUserId": auth_info["user_id"],
                "login_token": auth_info["token"],
                "userId": auth_info["user_id"],
                "user_id": auth_info["user_id"],
                "pageSize": LOCK_PAGE_SIZE,
                "pageIndex": page,
                "startTime": lock_start,
                "endTime": lock_end,
                "status": "1",
                "interceptType": "1"
            }
        }
        try:
            resp = requests.post(IMEI_LOCK_URL, json=payload, headers=JSON_HEADERS, timeout=30)
            res = resp.json()
            inner = res.get("_data", {})
            if str(inner.get("_ret")) == "0":
                page_list = inner.get("_data", {}).get("list", [])
                if not page_list:
                    break
                all_list.extend(page_list)
                if len(page_list) < LOCK_PAGE_SIZE:
                    break  # 已是最后一页
                page += 1
            else:
                failed = True
                if is_auth_failed(res):
                    auth_info["need_relogin"] = True
                print(f"[WARN] 拦截列表返回异常 ret={inner.get('_ret')} err={inner.get('_errStr')}")
                break
        except Exception as e:
            print(f"[ERROR] 拦截接口异常：{e}")
            failed = True
            break

    return all_list, failed

def filter_target_records(records):
    """
    已取消条码段正则限制：
    只要条码、IMEI 非空即纳入处理；同时透传原始记录（含记录ID，供后续自动通过使用）。
    """
    target_list = []
    for record in records:
        barcode = get_field(record, "seriesNum", "serialNumber", "barcode", "barCode", "sn").upper()
        imei = get_field(record, "imei", "IMEI")
        if barcode and imei:
            target_list.append({
                "intercept_barcode": barcode,
                "imei": imei,
                "record": record  # 原始拦截记录，后续通过接口需要其中的记录ID
            })
    return target_list

# ===================== 同机第二条条码匹配（取消强制同前缀，改为优先同前缀） =====================
def pick_best_barcode(exclude_barcode, candidates):
    """
    候选对比条码选择：
    1. 优先选与当前条码【同前缀】（前3位，如 010 / 020 / A01）的最新单，避免 020 比 010 跨模板；
    2. 没有同前缀候选时，再跨段取最新一条兜底。
    candidates: [{"barcode": ..., "time": ...}]
    返回 (选中条码, 是否同前缀)；无候选返回 ("", False)。
    """
    my_prefix = exclude_barcode.strip().upper()[:3]
    same, cross = [], []
    for c in candidates:
        if c["barcode"].strip().upper()[:3] == my_prefix:
            same.append(c)
        else:
            cross.append(c)

    pool, used_same = (same, True) if same else (cross, False)
    if not pool:
        return "", False
    pool.sort(key=lambda x: x.get("time") or "", reverse=True)
    return pool[0]["barcode"], used_same


def get_second_barcode(imei, exclude_barcode):
    exclude_clean = exclude_barcode.strip().upper()

    # 动态时间窗：近 RECORD_LOOKBACK_DAYS 天，避免写死固定日期导致跨年失效
    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=RECORD_LOOKBACK_DAYS)
    # 对齐前端抓包：时间格式用斜杠 YYYY/MM/DD，且 startTime/endTime 置于 payload 顶层
    start_time = start_dt.strftime("%Y/%m/%d 00:00:00")
    end_time = end_dt.strftime("%Y/%m/%d 23:59:59")

    all_records = []
    for page in range(0, MAX_HISTORY_PAGES):
        payload = {
            "_head": {"_interface": "deviceRecords"},
            "pageIndex": page,
            "pageSize": 100,
            "imei": imei,
            "serialNumber": "",
            "startTime": start_time,
            "endTime": end_time,
            "_param": {
                "loginUserId": auth_info["user_id"],
                "login_token": auth_info["token"],
                "userId": auth_info["user_id"],
                "user_id": auth_info["user_id"]
            }
        }

        try:
            resp = requests.post(DEVICE_RECORDS_URL, json=payload, headers=JSON_HEADERS, timeout=15)
            res = resp.json()
            inner = res.get("_data", {})
            page_data = inner.get("_data", {})
            page_list = page_data.get("list", [])
            if page == 0:
                total = page_data.get("total", page_data.get("totalCount", "?"))
                debug_print(f"[DEBUG] 历史查询 imei={imei} 时间窗={start_time} ~ {end_time} "
                            f"ret={inner.get('_ret')} err={inner.get('_errStr')} total={total} 本页={len(page_list)}")
            if not page_list:
                break
            all_records.extend(page_list)
            if len(page_list) < 100:
                break  # 最后一页
        except Exception as e:
            debug_print(f"[DEBUG] 第{page}页异常：{str(e)[:60]}")
            break

    # 兼容匹配：优先 IMEI / IMEI2；无蜂窝 IMEI 的设备（如 WiFi 平板）设备号存在 SN 类字段，一并兜底
    def norm_id(v):
        return str(v or "").strip().upper()

    target_id = norm_id(imei)
    SN_KEYS = ("sn", "serialNo", "serialNum", "deviceSn", "serialNumber",
               "productSn", "snCode", "snNum", "snNo", "deviceSerialNo")
    exact_records = []
    for rec in all_records:
        id_vals = [get_field(rec, "imei", "IMEI"), get_field(rec, "imei2", "IMEI2")]
        id_vals += [get_field(rec, k) for k in SN_KEYS]
        if any(norm_id(v) == target_id for v in id_vals):
            exact_records.append(rec)

    debug_print(f"[DEBUG] 设备号 {imei} 历史记录拉到 {len(all_records)} 条，IMEI/SN 匹配 {len(exact_records)} 条")

    # 匹配为 0 时打印每条记录的标量字段，定位设备号真实所在键名
    if not exact_records and all_records:
        for idx, rec in enumerate(all_records):
            brief = {}
            for k, v in rec.items():
                if isinstance(v, (str, int, float)) and str(v).strip() and len(str(v)) < 60:
                    brief[k] = str(v).strip()
            debug_print(f"[DEBUG] 历史记录{idx + 1}字段：{brief}")

    if len(exact_records) < 2:
        return ""

    # 排除当前拦截条码本身、以及无条码的记录；候选交由前缀优先规则挑选
    candidates = []
    for rec in exact_records:
        bc_raw = get_field(rec, "seriesNum", "serialNumber", "barcode", "barCode")
        bc_clean = bc_raw.upper()
        det_time = get_field(rec, "detectionTime", "time")

        if not bc_clean or bc_clean == exclude_clean:
            continue
        candidates.append({"barcode": bc_raw, "time": det_time})

    chosen, used_same = pick_best_barcode(exclude_clean, candidates)
    if not chosen:
        return ""
    debug_print(f"[DEBUG] 选中对比单 {chosen}（{'同前缀' if used_same else '跨段兜底'}）")
    return chosen

# ===================== 报告详情 =====================
def get_report_detail(serial_num):
    payload = {
        "_head": {"_interface": "getDetectRecordDetail"},
        "_param": {
            "loginUserId": auth_info["user_id"],
            "login_token": auth_info["token"],
            "userId": auth_info["user_id"],
            "user_id": auth_info["user_id"],
            "seriesNum": serial_num
        }
    }
    try:
        resp = requests.post(DETECT_DETAIL_URL, json=payload, headers=JSON_HEADERS, timeout=15)
        res = resp.json()
        inner = res.get("_data", {})
        if str(inner.get("_ret")) == "0" and inner.get("_data"):
            return inner["_data"][0]
    except Exception as e:
        debug_print(f"[DEBUG] 报告接口异常：{e}")
    return None

# ===================== 品类提取与负责人分配 =====================
# 经真实 JSON 确认：品类取报告顶级字段 className（取值如：手机/平板/笔记本/耳机...）
# 型号取 productName；classId 为品类编码备用。

def extract_category_raw(report_detail):
    """取 (品类名 className, 型号 productName)，品类直接是中文，无需猜字段"""
    if not report_detail:
        return "", ""
    cat_raw = str(report_detail.get("className", "") or "").strip()
    model_raw = str(report_detail.get("productName", "") or "").strip()
    debug_print(f"[DEBUG] className={cat_raw} / productName={model_raw} / classId={report_detail.get('classId', '')}")
    return cat_raw, model_raw

def classify_category(cat_raw, model_raw=""):
    """归类为 手机 / 平板 / 其他；className 已含中文时优先直接判断"""
    if "平板" in cat_raw:
        return "平板"
    if "手机" in cat_raw:
        return "手机"
    text = f"{cat_raw} {model_raw}".lower()
    if "ipad" in text or "tablet" in text or "平板" in text:
        return "平板"
    if "iphone" in text or "phone" in text or "手机" in text:
        return "手机"
    return "其他"

def get_owner(barcode, category):
    """
    负责人署名规则（category 形参保留兼容调用，当前不再按品类分叉）：
    020开头（全部品类）/ 字母+011开头（全部品类） → OWNER_A
    纯数字010开头 或 字母+010开头（全部品类）      → OWNER_B
    其余 → 空（不署名）
    具体姓名/手机号在 config.py 的 OWNER_MOBILES 中映射。
    """
    b = barcode.strip().upper()
    # OWNER_A：020 全部；字母+011 全部（不看品类）
    if b.startswith("020") or re.match(r"^[A-Z]+011", b):
        return "OWNER_A"
    # OWNER_B：纯数字 010，或 字母+010（不分品类）
    if b.startswith("010") or re.match(r"^[A-Z]+010", b):
        return "OWNER_B"
    return ""

# ===================== 检测项解析（前端同款，黑名单过滤） =====================
def parse_detect_items(report_detail):
    result_dict = {}
    if not report_detail:
        return result_dict

    blacklist = {
        # 前端隐藏的历史/属性项
        "屏幕维修情况", "售后案例情况",
        "颜色", "存储容量", "机身内存",
        "购买渠道", "型号", "制式", "网络类型",
        "保修期", "保修情况", "产品型号",
        # 与细项重复的综合结论项
        "iCloud账号", "开机情况", "边框背板", "机身弯曲",
        "屏幕外观", "屏幕显示", "触屏功能", "无线功能", "通话功能",
        "前置摄像头", "后置摄像头", "声音功能（麦克风/扬声器/听筒）",
        "屏幕传感器功能（光线/距离感应）", "侧键功能", "无线充电功能",
        "USB联机", "Face ID",
        "后摄像头维修情况", "前摄像头维修情况", "电池维修情况",
        "其他零部件情况", "后壳维修", "进水/受潮"
    }

    def clean_name(name):
        return name.strip().rstrip(":").rstrip("：")

    # 1. 外观细分检测项
    try:
        norm_template = json.loads(report_detail.get("detNormSnapshot", "[]"))
        norm_ids_raw = json.loads(report_detail.get("detNormIds", "[]"))
        norm_ids = set(str(x) for x in norm_ids_raw)

        for group in norm_template:
            group_name = clean_name(group.get("name", ""))
            if group_name in blacklist:
                continue
            items = group.get("item", [])
            for item in items:
                if str(item.get("id", "")) in norm_ids:
                    result_dict[group_name] = item.get("name", "")
                    break
    except Exception:
        pass

    # 2. 功能 / 拆修检测项
    try:
        eval_template = json.loads(report_detail.get("detEvaluateSnapshot2", "[]"))
        eval_ids_raw = json.loads(report_detail.get("detIds2", "[]"))
        eval_ids = set(str(item["id"]) for item in eval_ids_raw if "id" in item)

        for group in eval_template:
            group_name = clean_name(group.get("name", ""))
            if group_name in blacklist:
                continue
            items = group.get("item", [])
            for item in items:
                if str(item.get("id", "")) in eval_ids:
                    result_dict[group_name] = item.get("name", "")
                    break
    except Exception:
        pass

    return result_dict

def compare_reports(r1, r2):
    """
    返回 (diff, missing)：
    diff    : 双边都有有效值且值不同的【真差异】{项: (拦截报告值, 对比报告值)}
    missing : 单边缺失 / 单边空值项 {项: (方向, 现有值)}
              方向取 "仅拦截报告有" / "仅对比报告有"；不计为异常，单独列出。
    """
    diff = {}
    missing = {}
    for key in set(r1.keys()) | set(r2.keys()):
        v1 = str(r1.get(key, "")).strip()
        v2 = str(r2.get(key, "")).strip()
        if v1 and v2:
            if v1 != v2:
                diff[key] = (v1, v2)
        elif v1 and not v2:
            missing[key] = ("仅拦截报告有", v1)
        elif v2 and not v1:
            missing[key] = ("仅对比报告有", v2)
    return diff, missing

# ===================== 自动通过（待接入 imeiLockUpdate Payload） =====================
def auto_pass_record(record, reason):
    """
    TODO: 抓到 imeiLockUpdate 的 Payload 后补齐。
    触发条件（已确认规则）：差异项仅为「指纹按键外观」「其他按键」，
    且差异方向为 正常 ↔ 不支持。
    record 为拦截列表原始记录，其中含记录ID。
    """
    return False

# ===================== 业务处理 =====================
def process_intercept_record(target):
    intercept_barcode = target["intercept_barcode"]
    second_barcode = target["second_barcode"]
    imei = target["imei"]
    record = target["record"]

    detail1 = get_report_detail(intercept_barcode)
    detail2 = get_report_detail(second_barcode)

    # 任一报告拉取失败：跳过比对，但推送一条"报告获取失败"通知
    if not detail1 or not detail2:
        fail_names = []
        if not detail1:
            fail_names.append("拦截报告")
        if not detail2:
            fail_names.append("对比报告")
        fail_owner = ""
        if detail1:
            c_raw, m_raw = extract_category_raw(detail1)
            fail_owner = get_owner(intercept_barcode, classify_category(c_raw, m_raw))
        msg = f"""⚠️ 报告获取失败
时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
新条码：{intercept_barcode}
旧条码：{second_barcode or '-'}
IMEI：{imei}
失败项：{'、'.join(fail_names)}详情拉取失败，已跳过本次比对"""
        fail_at = [owner_to_mobile(fail_owner)] if fail_owner else []
        send_dingtalk(msg, fail_at)
        print(f"[SKIP] IMEI {imei} {'、'.join(fail_names)}获取失败，已推送失败通知")
        return

    dict1 = parse_detect_items(detail1)
    dict2 = parse_detect_items(detail2)

    # 品类取自拦截报告（当前被拦的那张）
    cat_raw, model_raw = extract_category_raw(detail1)
    category = classify_category(cat_raw, model_raw)
    owner = get_owner(intercept_barcode, category)

    # 控制台输出
    print("\n" + "=" * 60)
    print(f"IMEI：{imei}")
    print(f"拦截条码：{intercept_barcode}")
    print(f"对比条码：{second_barcode}")
    print(f"品类：{category}（原始值：{cat_raw or '未取到'} / 型号：{model_raw or '-'}）")
    print(f"负责人：{owner or '无'}")
    print("-" * 60)
    print("【拦截报告 - 检测项】")
    for k, v in dict1.items():
        print(f"  {k}：{v}")
    print("-" * 60)
    print("【对比报告 - 检测项】")
    for k, v in dict2.items():
        print(f"  {k}：{v}")
    print("-" * 60)

    diff_dict, missing_dict = compare_reports(dict1, dict2)

    type_desc = "非标验机报告" if intercept_barcode.startswith("020") else "标准检测报告"

    # 对比条码与拦截条码首字符类型不一致（一个数字开头、一个字母开头）：
    # 采集模板不同、检测项口径不可比，不自动逐项下结论，仅提示人工核实
    new_head = (intercept_barcode or "").strip()[:1]
    old_head = (second_barcode or "").strip()[:1]
    if new_head.isdigit() != old_head.isdigit():
        msg = f"""⚠️ 两份报告模板不同，请相关人员核实差异项目
设备信息
新条码：{intercept_barcode}（{new_head}开头）
旧条码：{second_barcode}（{old_head}开头）
IMEI：{imei}
品类：{category}
两份报告采集模板不同，检测项口径不一致，系统不自动判定，请人工核实差异。"""
        cross_at = [owner_to_mobile(owner)] if owner else []
        send_dingtalk(msg, cross_at)
        print(f"⚠️ 新条码({new_head}开头)与旧条码({old_head}开头)模板类型不同，已提示人工核实、不自动判定")
        print("=" * 60 + "\n")
        return

    def missing_lines(missing_map):
        out = []
        for idx, (item, (side, val)) in enumerate(missing_map.items(), 1):
            out.append(f"{idx}. {item}（{side}：{val}）")
        return out

    # 分支1：双边真差异 → 差异预警；若同时有单边缺少项，末尾附注（不计异常）
    if diff_dict:
        diff_lines = []
        for idx, (item, (v_new, v_old)) in enumerate(diff_dict.items(), 1):
            diff_lines.append(f"{idx}. {item}")
            diff_lines.append(f"  新条码：{v_new}")
            diff_lines.append(f"  旧条码：{v_old}")

        msg = f"""⚠️ IMEI检测差异预警
设备信息
新条码：{intercept_barcode}
旧条码：{second_barcode}
IMEI：{imei}
品类：{category}
报告类型：{type_desc}
差异项数：{len(diff_dict)}
差异明细
{chr(10).join(diff_lines)}"""
        if missing_dict:
            msg += (f"\n单边缺少项（仅供参考，不计异常，共{len(missing_dict)}项）"
                    f"\n{chr(10).join(missing_lines(missing_dict))}")

        diff_at = [owner_to_mobile(owner)] if owner else []
        send_dingtalk(msg, diff_at)
        extra = f"，另附单边缺少项 {len(missing_dict)} 项" if missing_dict else ""
        print(f"⚠️ 发现 {len(diff_dict)} 项差异{extra}，已推送钉钉（负责人：{owner or '无'}）")
        print("=" * 60 + "\n")
        return

    # 分支2：无真差异、但有单边缺少项 → 单独推一条"未识别出异常项目"
    if missing_dict:
        msg = f"""ℹ️ 未识别出异常项目
设备信息
新条码：{intercept_barcode}
旧条码：{second_barcode}
IMEI：{imei}
品类：{category}
报告类型：{type_desc}
缺少项数：{len(missing_dict)}
以下项目两份报告检测范围不一致（单边有值），未判定为异常
{chr(10).join(missing_lines(missing_dict))}"""
        missing_at = [owner_to_mobile(owner)] if owner else []
        send_dingtalk(msg, missing_at)
        print(f"ℹ️ 无异常差异，单边缺少项 {len(missing_dict)} 项，已推送钉钉")
        print("=" * 60 + "\n")
        return

    # 分支3：完全一致
    print("✅ 两份报告检测项无差异、无单边缺少项")
    print("=" * 60 + "\n")

# ===================== 主循环 =====================
def main():
    if not login():
        return

    if SEND_STARTUP_NOTIFY and in_broadcast_window():
        send_dingtalk(f"""服务启动成功
时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
轮询间隔：{POLL_INTERVAL}秒
告警冷却：{ALERT_COOLDOWN//60}分钟
播报时段：{BROADCAST_START_HOUR}:00-{BROADCAST_END_HOUR}:00（其余时间静默等待）
负责人规则：020/字母011→OWNER_A；纯数字010/字母010→OWNER_B""")

    print("[INFO] 监控启动，开始轮询拦截记录...\n")

    fail_streak = 0          # 连续硬失败轮次计数
    fail_alert_sent = False  # 本轮连续失败是否已告警，避免每轮刷屏
    last_window = None       # 记录上一轮是否在播报时段，仅在状态切换时打印

    while True:
        try:
            now_dt = datetime.now()
            in_window = in_broadcast_window(now_dt)

            # 仅在进入/离开播报时段时打印一次，避免时段外每轮刷屏
            if in_window != last_window:
                last_window = in_window
                if in_window:
                    print(f"[INFO] 进入播报时段 {BROADCAST_START_HOUR}:00-{BROADCAST_END_HOUR}:00，恢复处理与推送")
                else:
                    print(f"[INFO] {now_dt.strftime('%H:%M')} 非播报时段"
                          f"（{BROADCAST_START_HOUR}:00-{BROADCAST_END_HOUR}:00），静默等待，不处理不推送")

            # 非播报时段：不拉接口、不处理、不推送，静默等待下一周期
            if not in_window:
                time.sleep(POLL_INTERVAL)
                continue

            clean_expired_records()
            records, list_failed = get_intercept_list()

            # 检测到登录态失效标志：自动重新登录
            if auth_info.get("need_relogin"):
                print("[INFO] 检测到登录态失效，自动重新登录")
                login()

            # 连续 N 轮接口硬失败才告警一次；成功一轮立即复位
            if list_failed:
                fail_streak += 1
                if fail_streak >= FAIL_ALERT_THRESHOLD and not fail_alert_sent:
                    send_error_alert(
                        "拦截列表连续拉取失败",
                        f"已连续 {fail_streak} 轮（约 {POLL_INTERVAL * fail_streak} 秒）拉取 imeiLockList 失败，"
                        f"请检查网络 / 账号权限 / token 状态。"
                    )
                    fail_alert_sent = True
            else:
                fail_streak = 0
                fail_alert_sent = False

            targets = filter_target_records(records)
            print(f"[INFO] 本轮筛选到目标记录：{len(targets)}条")

            for target in targets:
                try:
                    bc = target["intercept_barcode"]
                    imei = target["imei"]
                    now = time.time()

                    if imei in processed_imei and now - processed_imei[imei] < ALERT_COOLDOWN:
                        continue

                    second_bc = get_second_barcode(imei, bc)
                    if not second_bc:
                        print(f"[INFO] 条码 {bc} / IMEI {imei} 未找到第二条对比条码，跳过\n")
                        processed_imei[imei] = now
                        continue

                    target["second_barcode"] = second_bc
                    process_intercept_record(target)
                    processed_imei[imei] = now
                except Exception as e:
                    # 单条记录异常隔离：只打印堆栈，不影响本轮其他记录，也不重复推钉钉
                    print(f"[ERROR] 单条处理异常 条码 {target.get('intercept_barcode', '?')} / "
                          f"IMEI {target.get('imei', '?')}：{e}")
                    traceback.print_exc()
                    continue

            print(f"[INFO] 本轮处理完成，{POLL_INTERVAL}秒后重试...\n")
            time.sleep(POLL_INTERVAL)

        except Exception as e:
            err_info = traceback.format_exc()
            send_error_alert("全局运行异常", err_info)
            print(f"[ERROR] 运行异常：{e}")
            login()
            time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
