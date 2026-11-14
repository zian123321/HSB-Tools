# -*- coding: utf-8 -*-
"""
本地配置示例。使用方法：
1. 复制本文件为 config.py（与 imei_monitor.py 同目录）；
2. 填入真实账号、接口地址、钉钉机器人 webhook、负责人手机号；
3. config.py 已在 .gitignore 中，不会被提交到仓库。

不想用 config.py 时，也可改为设置同名环境变量。
"""

# ---- 登录凭证 ----
USERNAME = "your_account@example.com"
PASSWORD = "your_password"

# ---- 业务接口地址（按实际系统替换；system_id 为登录参数） ----
SYSTEM_ID = "48"
JUMP_URL = "http%3A%2F%2Fexample.com%2F"
LOGIN_URL = "https://api.example.com/login"
IMEI_LOCK_URL = "https://api.example.com/imeiLockList"
DEVICE_RECORDS_URL = "https://api.example.com/deviceRecords"
DETECT_DETAIL_URL = "https://api.example.com/getDetectRecordDetail"
IMEI_LOCK_UPDATE_URL = "https://api.example.com/imeiLockUpdate"

# ---- 钉钉机器人 ----
# 机器人安全设置里的自定义关键词必须与 DINGTALK_KEYWORD 一致
DINGTALK_WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=YOUR_ACCESS_TOKEN"
DINGTALK_KEYWORD = "【检测监控】"
DINGTALK_ENABLED = False          # True=真实推送；False=仅控制台预览
SEND_STATUS_NOTIFY = True         # 错误/连续失败告警是否推送
SEND_STARTUP_NOTIFY = False       # 服务启动成功通知是否推送

# ---- 负责人标识 -> 钉钉绑定手机号 ----
# 真实 @ 需要本人在群内，且该手机号是其钉钉账号本人绑定号
# OWNER_A：020 / 字母+011 段；OWNER_B：纯数字010 / 字母+010 段
OWNER_MOBILES = {
    "OWNER_A": "13800000000",
    "OWNER_B": "13900000000",
}

# ---- 运行参数（可选覆盖，留注释即用脚本内默认值） ----
# BROADCAST_START_HOUR = 10
# BROADCAST_END_HOUR = 20
# POLL_INTERVAL = 60
# ALERT_COOLDOWN = 3600
# DEBUG_MODE = True
