#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
登录凭证更新小工具
==================
业务系统的登录凭证（loginUserId + loginToken）有效期由系统服务器决定，
我们无法从外部让它永久有效（也不能绕过系统鉴权）。到期后只需要一条命令更新：

用法（任选一种，Windows 推荐第 1 种，避开命令行长引号）：
  1. 把浏览器 Copy as cURL 的整条命令粘贴保存为同目录的 curl.txt，再运行：
       python 更新凭证.py curl.txt
     （或直接 python 更新凭证.py，工具会自动找同目录的 curl.txt）
  2. 命令行直接带参数：
       python 更新凭证.py "curl 'http://...' ... --data-raw '{...loginToken...}'"
  3. 不带参数运行，按提示粘贴 cURL，回车后按 Ctrl+Z 再回车（Windows）结束输入

原理：从你粘贴的命令里自动提取 loginUserId / loginToken，先用 wms/getLoginUserInfo
校验一次，校验通过后写入同目录的 凭证.json（拉数脚本启动时自动读取）。
三个系统共用同一凭证，一次更新全生效；源码文件不再被改写。

注意：本工具只写 凭证.json 一个文件，不改拉数脚本.py 等其它配置。
"""
import re, sys, os, json, time

BASE = os.path.dirname(os.path.abspath(__file__))
CRED_FILE = os.path.join(BASE, "凭证.json")


def read_input():
    # 1) 命令行参数：既可能是 cURL 字符串，也可能是一个文件路径（如 curl.txt）
    if len(sys.argv) > 1:
        arg = sys.argv[1].strip()
        if os.path.isfile(arg):
            print("已从文件读取：%s" % arg)
            return open(arg, encoding="utf-8", errors="replace").read()
        return arg
    # 2) 无参数：优先读取同目录下的 curl.txt（Windows 最省事的方式）
    for fname in ("curl.txt", "cURL.txt", "凭证.txt"):
        p = os.path.join(BASE, fname)
        if os.path.isfile(p):
            print("已从同目录 %s 读取 cURL 命令。" % fname)
            return open(p, encoding="utf-8", errors="replace").read()
    # 3) 交互式粘贴
    print("请粘贴浏览器里任意接口的 Copy as cURL 命令（含 loginToken/login_token 的那条），")
    print("然后按回车，再按 Ctrl+Z（Windows）/ Ctrl+D（Mac、Linux）回车结束输入：")
    return sys.stdin.read()


def extract(data):
    clean = re.sub(r"[\^\\]", "", data)   # 去掉 Windows cmd 的 ^ 转义符和 JSON 里的 \ 转义
    uid = re.search(r'loginUserId["\s]*[:=]\s*["\s]*(\d+)', clean)
    tok = re.search(r'login[_-]?[Tt]oken["\s]*[:=]\s*["\s]*([0-9a-fA-F]{16,})', clean)
    return (uid.group(1) if uid else None), (tok.group(1) if tok else None)


def validate(uid, tok):
    """用新凭证调一次 getLoginUserInfo，通过才写入。"""
    try:
        import requests
        url = "https://api.example-wms.com/wms/getLoginUserInfo"
        body = {
            "_head": {"_version": "0.01", "_msgType": "request",
                      "_timestamps": str(int(time.time())), "_interface": "loginuserinfo", "_remark": ""},
            "_param": {"loginUserId": uid, "loginToken": tok},
        }
        r = requests.post(url, json=body, headers={
            "Content-Type": "application/json",
            "Origin": "https://app.example-wms.com",
            "Referer": "https://app.example-wms.com/",
        }, timeout=30)
        d = r.json().get("_data") or {}
        if str(d.get("_errCode")) in ("0", "") and str(d.get("_errStr")) == "Success":
            return True, "校验通过"
        return False, "接口校验失败：" + json.dumps(d, ensure_ascii=False)[:200]
    except Exception as e:
        return False, "校验请求出错：" + str(e)


def write(uid, tok):
    cred = {"login_user_id": uid, "login_token": tok,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    tmp = CRED_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cred, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CRED_FILE)
    print("已写入 凭证.json：user_id=%s，token=%s…" % (uid, tok[:8]))


def main():
    data = read_input()
    uid, tok = extract(data)
    if not uid or not tok:
        print("没有从输入中解析出 loginUserId / loginToken。")
        print("请确认粘贴的是浏览器里「Copy as cURL」的整条命令，或至少包含如下字段的 JSON：")
        print('  {"_param": {"loginUserId": "…", "loginToken": "…"}}')
        sys.exit(1)
    ok, msg = validate(uid, tok)
    print("凭证校验：%s" % msg)
    if not ok:
        print("未写入。请重新登录系统后再抓 Copy as cURL；凭证由服务器签发，工具无法绕过有效期。")
        sys.exit(1)
    write(uid, tok)
    print("完成。常驻服务下一轮（或手动 python 拉数脚本.py）即使用新凭证。")


if __name__ == "__main__":
    main()
