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


def extract_checkin_meta(html):
    """
    从【登录后】的签到页 HTML 提取展示信息（当前积分、连续签到等）。
    只匹配显式格式（「当前积分：888」「积分：888」），避免把「积分规则」
    「获得积分 +10」等噪音当作积分；站点模板不同则抓不到，抓不到时返回空列表，
    不影响签到结果。
    返回格式：[("当前积分", "1,234"), ("连续签到", "5 天")]
    """
    text = re.sub(r"<[^>]+>", " ", html)
    # <!---- 注释 --> 内容也可能残留数字，先去除注释
    text = re.sub(r"<!--[\s\S]*?-->", " ", text)
    text = re.sub(r"\s+", " ", text)

    found = []

    def try_find(label, pattern, suffix=""):
        match = re.search(pattern, text)
        if match:
            found.append((label, match.group(1) + suffix))
            return True
        return False

    # 显式格式优先：「当前积分」命中后不再尝试「积分」；
    # 「积分」允许词与冒号间有空格（「积分 ： 88」），但冒号可省，
    # 数字前的干扰符号（如「获得积分 +10」的 +）会阻止匹配
    if not try_find("当前积分", r"当前积分[:：]?\s*([\d,]+)"):
        try_find("积分", r"积分\s*[:：]?\s*([\d,]+)")
    try_find("连续签到", r"连续签到\s*[:：]?\s*(\d+)\s*天", " 天")
    return found


# 用户名候选模式（按优先级）：
# 1. 该论坛程序（bbs1 同源）的个人信息卡结构，登录态各页面通用
# 2. 用户主页链接的文本
USERNAME_PATTERNS = (
    re.compile(r'class="user-name"[^>]*>([^<]{1,32})</a>'),
    re.compile(
        r'href="[^"]*/(?:user|member|profile|u)/\d+[^"]*"[^>]*>([^<]{1,32})</a>',
        re.IGNORECASE,
    ),
)


def extract_username(html):
    """从登录态页面提取用户名；页面无法解析（含未登录）时返回 None。"""
    for pattern in USERNAME_PATTERNS:
        match = pattern.search(html)
        if match:
            name = match.group(1).strip()
            if name:
                return name
    return None


def _debug_dump_checkin_area(html):
    """
    调试辅助：LINUXSB_DEBUG=1 时打印「当前积分」附近的页面片段（已脱敏），
    便于按实际页面结构调整用户名/积分解析正则。
    """
    if os.getenv("LINUXSB_DEBUG", "") != "1":
        return
    for keyword in ("当前积分", "积分"):
        pos = html.find(keyword)
        if pos != -1:
            start = max(0, pos - 250)
            segment = html[start:pos + 120]
            # 脱敏：CSRF token 与 cookie 相关值不落入日志
            segment = re.sub(r'name="_csrf"\s+value="[^"]*"', 'name="_csrf" value="***"', segment)
            print(f"[linux.sb][debug] {keyword} 附近页面片段：\n{segment}")
            break


def sign_in_account(cookie):
    """
    单个账号签到，返回 (成功与否, 多行结果摘要, 用户名或 None)。
    流程：GET 签到页拿 CSRF 与状态 -> 已签到则跳过 -> POST 签到 ->
    签到后再取一次签到页，提取用户名与「当前积分」「连续签到」等展示信息。

    注意：绝不把 cookie 内容/键名写入返回摘要——账号名一律用页面解析出的用户名，
    取不到时由调用方显示「账号 N」。

    CSRF token 取用顺序：
    1. 签到页 HTML 中的 name="_csrf" 隐藏字段（页面保留此写法时）
    2. cookie 中的 bbs_csrf 值（该论坛程序的 CSRF 凭据即存于此 cookie，
       部分站点版本页面不再渲染隐藏字段，直接提交 cookie 值即可）
    """
    csrf, checked_in = fetch_checkin_state(cookie)

    if csrf is None:
        # 页面未渲染 _csrf 字段时，回退到 cookie 中的 bbs_csrf（程序校验的就是它）
        csrf = (parse_cookies(cookie) or {}).get("bbs_csrf")
        if csrf:
            print("[linux.sb] 页面未发现 _csrf 字段，改用 cookie 中的 bbs_csrf 签到")

    if csrf is None:
        return False, "Cookie 已失效或页面结构变化：未找到 CSRF token，请重新登录 linux.sb 并更新 LINUXSB_COOKIE", None

    if checked_in:
        summary, username = _build_summary(["签到结果: 今日已签到，无需重复签到"], cookie)
        return True, summary, username

    result = send_checkin_request(cookie, csrf)
    if result.get("ok") in (1, True, "1", "true"):
        message = result.get("message", "")
        # POST 响应中 ok/message 之外的字段一并展示（不同站点版本字段名不同）
        extras = [f"{key}: {value}" for key, value in result.items()
                  if key not in ("ok", "message")]
        summary = "\n".join(extras)
        lines = [f"签到结果: 签到成功{f'（{message}）' if message else ''}"]
        if summary:
            lines.append(summary)
        built, username = _build_summary(lines, cookie)
        return True, built, username

    message = result.get("message", "")
    # 部分站点版本重复签到时返回 ok:0 +「已签到/已打卡/重复签到」，视为当日已签到（幂等）
    if any(word in message for word in ("已签到", "已打卡", "重复签到")):
        built, username = _build_summary(
            [f"签到结果: 今日已签到，无需重复签到（服务端：{message}）"], cookie
        )
        return True, built, username

    hint = "（Cookie 可能已失效，请重新登录 linux.sb 并更新 LINUXSB_COOKIE）" if "过期" in message else ""
    return False, f"签到失败：{message}{hint}", None


def _build_summary(lines, cookie):
    """追加登录态页面中的用户名/积分/连续签到概览，返回 (摘要, 用户名或 None)。"""
    html = ""
    try:
        response = requests.get(
            CHECKIN_URL, headers=PAGE_HEADERS, cookies=parse_cookies(cookie), timeout=30
        )
        if response.status_code == 200:
            html = response.text
    except requests.RequestException:
        pass

    username = None
    if html:
        username = extract_username(html)
        for label, value in extract_checkin_meta(html):
            lines.append(f"{label}: {value}")
        _debug_dump_checkin_area(html)
    # 抓不到概览信息时至少注明原因，避免通知里只有孤零零一行结果
    if len(lines) == 1:
        lines.append("概览信息: 未从页面取到（模板差异或 Cookie 权限不足）")
    return "\n".join(lines), username


def run():
    """
    执行 linux.sb 每日签到并推送通知，返回进程退出码（全部成功为 0，否则为 1）。
    多个账号（& 分隔）依次签到：单个账号失败不中断其余账号。
    通知格式对齐 nodeseek_daily：顶部任务执行时间 + 站点分段 + 各账号概览行。
    """
    raw_cookies = os.getenv("LINUXSB_COOKIE", "").strip()
    if not raw_cookies:
        # 与 DEEPFLOOD_COOKIE 一致：未配置即视为未启用该站，静默跳过、不算失败
        print("[linux.sb] 未配置 LINUXSB_COOKIE，跳过 linux.sb 签到")
        return 0

    # 记录任务启动时刻，作为通知顶部时间，早于各账号签到时间，符合直觉的时间轴顺序
    task_started_at = time.strftime("%Y-%m-%d %H:%M:%S")

    # 签到前随机延迟，拉开与 NodeSeek 等站的执行时间间隔
    gap = random.randint(SITE_GAP_MIN, SITE_GAP_MAX)
    print(f"[linux.sb] 等待 {gap} 秒后再开始，避免连续签到被风控")
    time.sleep(gap)

    cookie_list = raw_cookies.split("&")
    print(f"[linux.sb] 共 {len(cookie_list)} 个账号开始签到")

    results = []
    sections = []
    for idx, cookie in enumerate(cookie_list, start=1):
        # 记录本账号开始签到的时间，写入通知，便于核对执行时点
        started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            success, summary, username = sign_in_account(cookie)
        except Exception as error:  # 网络异常等：单个账号失败不影响其余账号
            success, summary, username = False, f"签到异常：{error}", None
        # 账号标识优先用页面解析的用户名，取不到才显示「账号 N」；绝不使用 cookie 内容
        display = username or f"账号 {idx}"
        print(f"[linux.sb] {display}：{summary}")
        results.append((success, summary))
        sections.append(
            f"{display}\n"
            f"执行时间: {started_at}\n"
            f"{summary}"
        )

    all_success = all(success for success, _ in results)
    title = "LinuxSB 每日任务" + ("" if all_success else "（签到异常）")
    content = (
        f"执行时间: {task_started_at}\n"
        "【linux.sb】\n"
        + "\n\n".join(sections)
    )
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