# -- coding: utf-8 --
"""
linux.sb（烧饼社区）每日自动签到脚本。

与 nodeseek_daily.py 同属本仓库的多站签到体系：NodeSeek、DeepFlood（浏览器签到）
与 linux.sb（纯 requests 签到）可顺序执行，共用 notify.py 推送通知。

linux.sb 无 Cloudflare 防护，签到基于 Cookie + CSRF token，接口协议对照
qd-today/templates 的 Linux_SB.har 官方模板确认：
1. GET  https://linux.sb/daily_checkin  携带 Cookie，从页面 HTML 提取
   - CSRF token：<input type="hidden" name="_csrf" value="...">
   - 签到状态：页面文本包含「今日已签到」表示今日已签到，可跳过
2. POST https://linux.sb/daily_checkin  携带 Cookie，表单 _csrf=xxx
   - 请求头需带 X-Requested-With: XMLHttpRequest、Referer 等
   - 响应为 JSON：{"ok":1,"message":"..."} 成功；{"ok":0,"message":"请求已过期"} 失败

环境变量：
- LINUXSB_COOKIE：登录 Cookie；多账号用 & 分隔，依次签到、单账号失败不中断
- SITE_GAP_MIN / SITE_GAP_MAX：签到前随机延迟范围（秒，默认 60-180），
  与 nodeseek_daily.py 的站间延迟共用同一对变量，降低被风控判为批量行为的概率
- 通知渠道配置见 notify.py（TG_BOT_TOKEN、WECOM_WEBHOOK 等，全部可选）
"""
import os
import re
import random
import time
import traceback

import requests

import notify

# 本地调试时从 .env 读取配置；GitHub Actions 环境直接使用注入的环境变量
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

BASE_URL = "https://linux.sb"
CHECKIN_URL = BASE_URL + "/daily_checkin"

# 从签到页 HTML 提取 CSRF token
CSRF_RE = re.compile(r'name="_csrf"\s+value="([^"]+)"')
# 成功签到后页面出现的状态文字
CHECKED_IN_TEXT = "今日已签到"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
)

PAGE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "User-Agent": USER_AGENT,
}

POST_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/x-www-form-urlencoded",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": CHECKIN_URL,
    "User-Agent": USER_AGENT,
}


def _env_int(name, default):
    """读取整型环境变量，非法或未设置时返回默认值。"""
    try:
        return int(os.environ.get(name))
    except (TypeError, ValueError):
        return default


# 与 nodeseek_daily.py 共用的站间随机延迟范围（秒）
SITE_GAP_MIN = _env_int("SITE_GAP_MIN", 60)
SITE_GAP_MAX = _env_int("SITE_GAP_MAX", 180)


def parse_cookies(raw_cookie):
    """把形如 'a=1; b=2' 的 Cookie 字符串解析为字典。"""
    cookies = {}
    for item in raw_cookie.strip().split(";"):
        if "=" in item:
            key, value = item.split("=", 1)
            cookies[key.strip()] = value.strip()
    return cookies


def fetch_checkin_state(cookie):
    """
    访问签到页，返回 (csrf_token, 是否已签到)。
    cookie 失效时页面无 CSRF token，csrf 为 None。
    """
    response = requests.get(
        CHECKIN_URL, headers=PAGE_HEADERS, cookies=parse_cookies(cookie), timeout=30
    )
    if response.status_code != 200:
        raise RuntimeError(f"获取签到页面失败，HTTP {response.status_code}")

    html = response.text
    match = CSRF_RE.search(html)
    csrf = match.group(1) if match else None
    checked_in = CHECKED_IN_TEXT in html
    return csrf, checked_in


def send_checkin_request(cookie, csrf):
    """执行签到 POST 请求，返回服务端 JSON 响应（解析失败抛异常）。"""
    response = requests.post(
        CHECKIN_URL,
        headers=POST_HEADERS,
        cookies=parse_cookies(cookie),
        data={"_csrf": csrf},
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(f"签到请求失败，HTTP {response.status_code}")
    return response.json()


def sign_in_account(cookie):
    """
    单个账号签到，返回 (成功与否, 结果摘要)。
    流程：GET 签到页拿 CSRF 与状态 -> 已签到则跳过 -> POST 签到。
    """
    csrf, checked_in = fetch_checkin_state(cookie)

    if csrf is None:
        # 页面无 CSRF token，通常是因为 Cookie 失效被重定向到了登录页
        return False, "Cookie 已失效：未找到 CSRF token，请重新登录 linux.sb 并更新 LINUXSB_COOKIE"

    if checked_in:
        return True, "今日已签到，无需重复签到"

    result = send_checkin_request(cookie, csrf)
    if result.get("ok") in (1, True, "1", "true"):
        return True, f"签到成功：{result.get('message', '')}"

    message = result.get("message", "")
    hint = "（Cookie 可能已失效，请重新登录 linux.sb 并更新 LINUXSB_COOKIE）" if "过期" in message else ""
    return False, f"签到失败：{message}{hint}"


def run():
    """
    执行 linux.sb 每日签到并推送通知，返回进程退出码（全部成功为 0，否则为 1）。
    多个账号（& 分隔）依次签到：单个账号失败不中断其余账号。
    """
    raw_cookies = os.getenv("LINUXSB_COOKIE", "").strip()
    if not raw_cookies:
        notify.send("LinuxSB 每日任务失败", "未配置 LINUXSB_COOKIE（浏览器登录 linux.sb 后复制 Cookie）")
        return 1

    # 签到前随机延迟，拉开与 NodeSeek 等站的执行时间间隔
    gap = random.randint(SITE_GAP_MIN, SITE_GAP_MAX)
    print(f"[linux.sb] 等待 {gap} 秒后再开始，避免连续签到被风控")
    time.sleep(gap)

    cookie_list = raw_cookies.split("&")
    print(f"[linux.sb] 共 {len(cookie_list)} 个账号开始签到")

    results = []
    for idx, cookie in enumerate(cookie_list, start=1):
        name = cookie.split(";", 1)[0].split("=", 1)[0] or f"账号{idx}"
        try:
            success, summary = sign_in_account(cookie)
        except Exception as error:  # 网络异常等：单个账号失败不影响其余账号
            success, summary = False, f"签到异常：{error}"
        print(f"[linux.sb] 账号 {idx}（{name}）：{summary}")
        results.append((success, f"账号 {idx}（{name}）：{summary}"))

    all_success = all(success for success, _ in results)
    title = "LinuxSB 每日任务" + ("" if all_success else "（签到异常）")
    content = "\n".join(f"- {summary}" for _, summary in results)
    notify.send(title, content)
    return 0 if all_success else 1


def main():
    """顶层入口，捕获所有未预期异常，确保通知一定能发出。"""
    try:
        return run()
    except Exception:
        print("脚本发生未预期异常:")
        traceback.print_exc()
        notify.send("LinuxSB 每日任务异常", "脚本执行中断，请查看日志排查")
        return 1


if __name__ == "__main__":
    exit(main())