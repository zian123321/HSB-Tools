#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自动登录刷新凭证
================
用浏览器自动化自动登录业务系统，抓新 token 写入 凭证.json。

首次使用：
  1. pip install playwright
  2. playwright install chromium
  3. 在 config.json 里加两行：
       "login_username": "你的工号/账号",
       "login_password": "你的密码"
  4. python 自动登录.py        （先看着浏览器跑一次，确认能通）
  5. 跑通后把下面 HEADLESS 改成 True（后台静默跑）

定时每天早8点跑：
  方案A：Windows 任务计划程序，每天 08:00 执行 python 自动登录.py
  方案B：把本脚本加到常驻看板服务里，每天第一轮拉数前先刷新
"""
import json, os, time, re

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
CRED_PATH = os.path.join(BASE, "凭证.json")

# 首次调试先设 False（能看到浏览器窗口），跑通后改 True 后台静默跑
HEADLESS = False


def load_config():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        cfg = json.load(open(CONFIG_PATH, encoding="utf-8"))
    return cfg


def find_token_in_data(obj, depth=0):
    """递归在 localStorage/cookie 数据里找 token 和 user_id"""
    token = None
    user_id = None
    if depth > 5:
        return None, None
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = k.lower()
            if 'token' in kl and isinstance(v, str) and len(v) >= 16:
                token = v
            if ('userid' in kl or 'user_id' in kl or 'loginuserid' in kl) and isinstance(v, (str, int)):
                user_id = str(v)
            t2, u2 = find_token_in_data(v, depth + 1)
            if t2 and not token:
                token = t2
            if u2 and not user_id:
                user_id = u2
    elif isinstance(obj, str):
        # 可能整个值是个 JSON 字符串
        try:
            inner = json.loads(obj)
            return find_token_in_data(inner, depth + 1)
        except:
            pass
    return token, user_id


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("未安装 playwright，请先跑：pip install playwright && playwright install chromium")
        return

    cfg = load_config()
    username = cfg.get("login_username", "")
    password = cfg.get("login_password", "")
    login_url = cfg.get("login_url", "https://app.example-detect.com/")

    if not username or not password:
        print("请先在 config.json 里配置 login_username 和 login_password")
        return

    print("开始自动登录：%s" % login_url)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = browser.new_context()
        page = context.new_page()

        try:
            page.goto(login_url, timeout=30000)
            page.wait_for_load_state("networkidle", timeout=15000)
            time.sleep(2)
        except Exception as e:
            print("打开页面超时：%s" % e)

        # 自动找用户名输入框（按优先级试）
        user_filled = False
        user_selectors = [
            'input[name="username"]',
            'input[name="userName"]',
            'input[name="loginName"]',
            'input[name="account"]',
            'input[placeholder*="账号"]',
            'input[placeholder*="用户"]',
            'input[placeholder*="工号"]',
            'input[placeholder*="手机"]',
            'input[type="text"]:visible',
        ]
        for sel in user_selectors:
            try:
                el = page.wait_for_selector(sel, timeout=2000)
                if el and el.is_visible():
                    el.fill(username)
                    user_filled = True
                    print("用户名输入框：%s" % sel)
                    break
            except:
                continue

        if not user_filled:
            print("没找到用户名输入框，当前页面标题：%s" % page.title())
            print("请把登录页截图发给我，我调整选择器")
            browser.close()
            return

        # 填密码
        pwd_filled = False
        try:
            page.fill('input[type="password"]', password)
            pwd_filled = True
            print("密码输入框：input[type=password]")
        except:
            pass

        if not pwd_filled:
            print("没找到密码输入框")
            browser.close()
            return

        # 点登录按钮
        clicked = False
        btn_selectors = [
            'button:has-text("登录")',
            'button:has-text("登 录")',
            'button:has-text("Login")',
            'button[type="submit"]',
            'input[value*="登录"]',
        ]
        for sel in btn_selectors:
            try:
                page.click(sel, timeout=2000)
                clicked = True
                print("登录按钮：%s" % sel)
                break
            except:
                continue

        if not clicked:
            print("没找到登录按钮")
            browser.close()
            return

        # 等登录完成（页面跳转 + 网络空闲）
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except:
            pass
        time.sleep(3)

        print("登录后页面 URL：%s" % page.url)

        # 从 localStorage / sessionStorage / cookie 里抓 token
        token = None
        user_id = None

        # localStorage
        try:
            ls_raw = page.evaluate("() => JSON.stringify(window.localStorage)")
            ls_obj = json.loads(ls_raw)
            t, u = find_token_in_data(ls_obj)
            if t:
                token = t
                print("从 localStorage 找到 token")
            if u:
                user_id = u
        except Exception as e:
            print("localStorage 读取失败：%s" % e)

        # sessionStorage
        if not token:
            try:
                ss_raw = page.evaluate("() => JSON.stringify(window.sessionStorage)")
                ss_obj = json.loads(ss_raw)
                t, u = find_token_in_data(ss_obj)
                if t:
                    token = t
                    print("从 sessionStorage 找到 token")
                if u and not user_id:
                    user_id = u
            except:
                pass

        # cookies
        if not token:
            try:
                cookies = context.cookies()
                t, u = find_token_in_data({c["name"]: c["value"] for c in cookies})
                if t:
                    token = t
                    print("从 cookie 找到 token")
                if u and not user_id:
                    user_id = u
            except:
                pass

        # 调试：如果还没找到，把所有数据 dump 出来
        if not token:
            print("\n=== 没自动找到 token，调试信息如下 ===")
            try:
                print("localStorage:", page.evaluate("() => JSON.stringify(window.localStorage)"))
            except:
                pass
            try:
                print("cookies:", context.cookies())
            except:
                pass
            print("请把上面的调试信息发给我，我调整抓取逻辑")
            browser.close()
            return

        browser.close()

    # 写入凭证.json（原子写）
    cred = {
        "login_user_id": user_id or "",
        "login_token": token,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")
    }
    tmp = CRED_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cred, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CRED_PATH)
    print("\n自动登录成功！新凭证已写入：")
    print("  user_id = %s" % user_id)
    print("  token   = %s…" % token[:8])
    print("  时间    = %s" % cred["updated_at"])
    print("\n常驻服务下一轮拉数即使用新凭证。")


if __name__ == "__main__":
    main()
