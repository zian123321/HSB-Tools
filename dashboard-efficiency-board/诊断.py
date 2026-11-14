#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
看板拉数 · 一键诊断脚本（只读，不改任何缓存/看板）
=====================================================
用途：常驻服务反复报「退出码 1」但看不到原因时，运行本脚本，
     它分别对三个数据源发一次第一页请求，把真实报错分类写进
     同目录「诊断结果.txt」，同时打印到窗口。

用法：python 诊断.py
然后把 诊断结果.txt 的内容（或窗口截图）发回即可定位。
"""
import os, sys, json, time, datetime, traceback

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"

# 读取凭证（与主脚本同一来源 凭证.json）
uid, tok = "", ""
cred_path = os.path.join(BASE_DIR, "凭证.json")
if os.path.exists(cred_path):
    try:
        c = json.load(open(cred_path, encoding="utf-8"))
        uid, tok = str(c.get("login_user_id", "")), str(c.get("login_token", ""))
    except Exception:
        pass

NOW = datetime.datetime.now()
TODAY0 = NOW.replace(hour=0, minute=0, second=0, microsecond=0)

# 占位地址与仓库编号，接入真实环境时与 拉数脚本.py 的 SYSTEMS 保持一致
SOURCES = [
    {
        "name": "收货记录",
        "url": "https://api.example-wms.com/wms/receivelog",
        "interface": "your-wms-receive-interface",
        "headers": {"Content-Type": "application/json", "Origin": "https://app.example-wms.com",
                    "Referer": "https://app.example-wms.com/", "User-Agent": UA},
        "auth": {"loginUserId": uid, "loginToken": tok},
        "params": {"depotId": "DEPOT_ID_1"},
        "range_fields": None,
        "list_field": "yourReceiveListField",
    },
    {
        "name": "验货拆包",
        "url": "https://api.example-xy.com/xywms/getcheckinparcels",
        "interface": "your-xy-delivery-interface",
        "headers": {"Content-Type": "application/json", "Origin": "https://app.example-xy.com",
                    "Referer": "https://app.example-xy.com/", "User-Agent": UA},
        "auth": {"loginUserId": uid, "loginToken": tok},
        "params": {"depotId": "DEPOT_ID_2", "logisticsNum": None},
        "range_fields": ["operateStartTime", "operateEndTime"],
        "range_fmt": "%Y-%m-%d %H:%M:%S",
        "list_field": "yourParcelListField",
    },
    {
        "name": "真机拍照",
        "url": "https://api.example-detect.com/api/autoPhotoList",
        "interface": "your-photo-interface",
        "headers": {"Content-Type": "application/json", "Origin": "https://app.example-detect.com",
                    "Referer": "https://app.example-detect.com/", "User-Agent": UA},
        "auth": {"user_id": uid, "userId": uid, "loginUserId": uid, "login_token": tok},
        "params": {"serialNumber": ""},
        "range_fields": ["startTime", "endTime"],
        "range_fmt": "%Y/%m/%d %H:%M:%S",
        "list_field": "list",
    },
]


def build_body(src):
    param = dict(src["auth"])
    param.update({"pageIndex": "0", "pageSize": "100"})
    param.update({k: v for k, v in src["params"].items() if v is not None})
    if src.get("range_fields"):
        fmt = src.get("range_fmt", "%Y-%m-%d %H:%M:%S")
        param[src["range_fields"][0]] = TODAY0.strftime(fmt)
        param[src["range_fields"][1]] = NOW.strftime(fmt)
    return {
        "_head": {"_version": "0.01", "_msgType": "request",
                  "_timestamps": str(int(time.time())), "_interface": src["interface"], "_remark": ""},
        "_param": param,
    }


def probe(src, verify_ssl=True):
    """发第一页请求，返回结果 dict。"""
    import requests
    t0 = time.time()
    body = build_body(src)
    try:
        r = requests.post(src["url"], headers=src["headers"], json=body,
                          timeout=15, verify=verify_ssl)
        ms = int((time.time() - t0) * 1000)
        out = {"http_status": r.status_code, "elapsed_ms": ms}
        try:
            j = r.json()
        except Exception:
            out["non_json"] = r.text[:400]
            return out
        d = j.get("_data") or {}
        out["errCode"] = d.get("_errCode")
        out["errStr"] = d.get("_errStr")
        lst = ((d.get("_data") or {}).get(src["list_field"])) or []
        out["page_rows"] = len(lst)
        if lst:
            out["first_record_time"] = (lst[0].get("createTime") or lst[0].get("addTime") or "")
        return out
    except Exception as e:
        out = {"exception_type": type(e).__name__, "exception_msg": str(e)[:500],
               "elapsed_ms": int((time.time() - t0) * 1000)}
        return out


def classify(src, res, verify_ssl=True):
    if "exception_type" in res:
        t = res["exception_type"]
        if "SSL" in t or "Certificate" in t:
            return "证书/TLS 问题（公司代理或本机证书拦截了 https 拍照接口）"
        if "Connection" in t:
            return "网络连不上该接口（断网/DNS/防火墙/需连公司内网）"
        if "Timeout" in t:
            return "请求超时（15 秒无响应，网络慢或服务端卡住）"
        return "未捕获异常：%s" % t
    ec, es = str(res.get("errCode") or ""), str(res.get("errStr") or "")
    if ec in ("0", "") and res.get("page_rows", 0) >= 0:
        return "接口连通正常（HTTP %s，本页 %s 条）" % (res.get("http_status"), res.get("page_rows"))
    if any(k in es for k in ("登录", "凭证", "token", "Token", "login", "Login")):
        return "凭证失效（需换证）"
    return "业务错误：errCode=%s errStr=%s" % (ec, es)


def main():
    lines = []
    def w(s=""):
        print(s)
        lines.append(s)

    w("=" * 64)
    w("看板拉数诊断  %s" % NOW.strftime("%Y-%m-%d %H:%M:%S"))
    w("凭证 user_id=%s  token=%s…（长度 %d）" % (uid, tok[:8], len(tok)))
    w("=" * 64)

    for src in SOURCES:
        w("\n【%s】%s" % (src["name"], src["url"]))
        res = probe(src, verify_ssl=True)
        w("  常规校验结果：%s" % json.dumps(res, ensure_ascii=False))
        w("  判断：%s" % classify(src, res, True))
        # 拍照是 https，若证书报错，再跳过证书校验试一次，区分是证书拦截还是服务端拒绝
        if src["url"].startswith("https") and "exception_type" in res and ("SSL" in res["exception_type"] or "Certificate" in res["exception_type"]):
            try:
                import urllib3
                urllib3.disable_warnings()
            except Exception:
                pass
            res2 = probe(src, verify_ssl=False)
            w("  跳过证书校验复测：%s" % json.dumps(res2, ensure_ascii=False))
            if "exception_type" not in res2:
                w("  判断：跳过证书后可通 → 确为本机证书/代理拦截，需在电脑装公司根证书或让 requests 信任该证书")
            else:
                w("  判断：跳过证书仍失败 → 不是证书问题，是网络或服务端问题")

    w("\n" + "-" * 64)
    w("结论速查：")
    w("  · 出现「凭证失效/3001/登陆超时」→ 浏览器登录后 Copy as cURL 存 curl.txt，运行 python 更新凭证.py curl.txt")
    w("  · 出现「网络连不上」→ 确认电脑连着公司内网/WiFi，能在浏览器打开对应系统页面")
    w("  · 出现「证书/TLS 问题」→ 拍照接口 https 被本机拦截，把本文件发回给助手改代码")
    w("  · 三个都「连通正常」但看板不更新 → 把本文件发回，进一步查分页/字段解析")

    out_path = os.path.join(BASE_DIR, "诊断结果.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("\n诊断结果已写入：%s" % out_path)


if __name__ == "__main__":
    main()
