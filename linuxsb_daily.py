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
- LINUXSB_COOKIE：登录 Cookie；多账号用 & 分隔，依次签到、单账号失败不中断。
  优先使用；失效或未配置时，若提供 LINUXSB_ACCOUNT 则自动用账号密码登录
- LINUXSB_ACCOUNT：账号密码凭据（JSON：{"username":"...","password":"..."}），
  作为 Cookie 的兜底登录方式。Cookie 有效时不使用；Cookie 失效或缺失时
  用浏览器自动登录（算术题验证码自动解析，PoW 由页面自身 JS 完成）
- SITE_GAP_MIN / SITE_GAP_MAX：签到前随机延迟范围（秒，默认 60-180），
  与 nodeseek_daily.py 的站间延迟共用同一对变量，降低被风控判为批量行为的概率
- 通知渠道配置见 notify.py（TG_BOT_TOKEN、WECOM_WEBHOOK 等，全部可选）
"""
import json
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


def env_bool(name, default=False):
    """解析布尔型环境变量，true/1/yes/on/y（大小写不敏感）为真，其余为假。"""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("true", "1", "yes", "on", "y")


def load_account_creds():
    """
    解析 LINUXSB_ACCOUNT（JSON：{"username": "...", "password": "..."}）。
    未配置或格式非法时返回 None，不中断签到流程（仅影响兜底登录是否可用）。
    """
    raw = os.getenv("LINUXSB_ACCOUNT", "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", "")).strip()
    except (ValueError, AttributeError):
        print("[linux.sb] LINUXSB_ACCOUNT 不是合法 JSON，请使用 "
              '格式 {"username": "...", "password": "..."}')
        return None
    if not username or not password:
        print("[linux.sb] LINUXSB_ACCOUNT 缺少 username 或 password 字段")
        return None
    return {"username": username, "password": password}


# 算术题验证码的运算符映射（无依赖的简单四则运算）
_CAPTCHA_OPS = {
    "+": lambda a, b: a + b,
    "-": lambda a, b: a - b,
    "×": lambda a, b: a * b,
    "x": lambda a, b: a * b,
    "*": lambda a, b: a * b,
    "÷": lambda a, b: a / b,
    "/": lambda a, b: a / b,
}


def solve_captcha_question(text):
    """
    解析登录页算术题验证码题面（如「9 × 4 = ?」「7 + 3 = ?」）并计算结果。
    除法结果要求整除（常见题面都是整除），否则该题面无解会抛异常。
    """
    match = re.search(r"(\d+)\s*([+\-×x*/÷])\s*(\d+)\s*=\s*\?", text)
    if not match:
        raise ValueError(f"无法解析算术题题面：{text!r}")
    a, op, b = int(match.group(1)), match.group(2), int(match.group(3))
    result = _CAPTCHA_OPS[op](a, b)
    if isinstance(result, float) and not result.is_integer():
        raise ValueError(f"算术题非整除结果：{text}")
    return str(int(result))


# 与 nodeseek_daily.py 共用的站间随机延迟范围（秒）
SITE_GAP_MIN = _env_int("SITE_GAP_MIN", 60)
SITE_GAP_MAX = _env_int("SITE_GAP_MAX", 180)


def parse_cookies(raw_cookie):
    """
    把 Cookie 字符串解析为字典（与 nodeseek_daily.py 同策略）。

    cookie 值本身可能含分号（例如被截断的 JSON），无脑按分号切分会把一个
    cookie 拆成两半。因此逐段判断：某段等号左侧不是合法 cookie 名时，
    视为上一个 cookie 值的延续并拼回去。
    """
    name_pattern = re.compile(r"^[A-Za-z0-9!#$%&'*+\-.^_`|~]+$")
    cookies = {}
    current = None
    for chunk in re.split(r"[;\r\n]+", raw_cookie or ""):
        segment = chunk.strip()
        if not segment:
            continue
        if "=" in segment:
            key, value = segment.split("=", 1)
            key = key.strip()
            if key and name_pattern.match(key):
                cookies[key] = value.strip()
                current = key
                continue
        # 残段：不是新 cookie（如值内含分号），拼回上一个 cookie 的值
        if current is not None:
            cookies[current] += ";" + segment
    return cookies


def detect_chrome_major_version():
    """
    探测 Chrome 大版本号：优先读环境变量 CHROME_MAJOR_VERSION，
    否则解析 google-chrome --version（与 nodeseek_daily.py 策略一致）。
    未探测到时返回 None，由 undetected-chromedriver 自动匹配。
    """
    import subprocess

    raw = os.getenv("CHROME_MAJOR_VERSION", "").strip()
    if raw.isdigit():
        return int(raw)
    try:
        result = subprocess.run(
            ["google-chrome", "--version"],
            capture_output=True, text=True, timeout=10,
        )
        match = re.search(r"(\d+)\.", result.stdout)
        if match:
            return int(match.group(1))
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def browser_login(creds):
    """
    用浏览器自动登录 linux.sb，返回登录后的完整 Cookie 字符串。

    登录页的三道验证均由页面自身完成大半：校验码 PoW 由前端 JS 计算，
    脚本只需填写算术题答案（题面可解析计算）并保持蜜罐字段为空。
    浏览器库（selenium/undetected_chromedriver 等）函数内局部导入，
    保证未安装浏览器依赖时纯 requests 签到仍可正常使用。
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    import undetected_chromedriver as uc

    options = uc.ChromeOptions()
    # linux.sb 无 Cloudflare 防护，无头模式即可通过（Actions 无显示环境必须无头）；
    # 需要调试时设 HEADLESS=false 配合 xvfb 使用
    if env_bool("HEADLESS", True):
        options.add_argument("--headless=new")
    else:
        options.add_argument("--window-size=1920,1080")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")

    # runner 预装的 Chrome 往往落后于最新 chromedriver，版本不匹配会直接抛
    # SessionNotCreatedException；显式传入实际大版本号强制取匹配的驱动
    driver_kwargs = {}
    version_main = detect_chrome_major_version()
    if version_main:
        print(f"[linux.sb] 检测到 Chrome 大版本 {version_main}，使用匹配的驱动")
        driver_kwargs["version_main"] = version_main

    driver = uc.Chrome(options=options, **driver_kwargs)
    try:
        driver.get(f"{BASE_URL}/login")
        wait = WebDriverWait(driver, 30)

        # 填入用户名与密码
        username_input = wait.until(EC.presence_of_element_located((By.NAME, "username")))
        username_input.clear()
        username_input.send_keys(creds["username"])
        password_input = driver.find_element(By.NAME, "password")
        password_input.clear()
        password_input.send_keys(creds["password"])

        # 读取算术题题面并填入计算结果（例如「9 × 4 = ?」-> 36）
        question_el = wait.until(
            EC.visibility_of_element_located((By.CLASS_NAME, "native-captcha-question"))
        )
        answer = solve_captcha_question(question_el.text)
        answer_input = driver.find_element(By.NAME, "native_captcha_answer")
        answer_input.clear()
        answer_input.send_keys(answer)

        # 蜜罐字段 native_captcha_company 保持为空（伪装真人必须留空）
        # 点击登录并等待跳转（登录成功会离开 /login 页面）
        submit = driver.find_element(By.CSS_SELECTOR, "form button[type=submit], form button")
        submit.click()
        wait.until(lambda d: "login" not in d.current_url)

        # 只导出当前站点域的 cookie：get_cookies() 会返回全部域名（含 CDN、
        # 统计等其他域的同名 cookie），一并注入 requests 会互相覆盖导致登录态丢失
        host = BASE_URL.split("://", 1)[1]
        cookies = [
            c for c in driver.get_cookies()
            if c.get("domain", "").lstrip(".").endswith(host)
        ]
        if not cookies:
            raise RuntimeError("登录后未取到任何 Cookie，可能登录被防爬拦截")
        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        return cookie_str
    except Exception as exc:
        # 不打印页面源码：公开仓库日志可见，登录后页面可能含个人字段
        raise RuntimeError(f"浏览器登录失败：{exc}") from exc
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def ensure_valid_cookie(raw_cookie, creds):
    """
    Cookie 优先：现有 Cookie 有效则原样返回。
    Cookie 无效或未配置、且提供了账号密码时，用浏览器登录替换并返回新 Cookie；
    无可替代凭据时返回原值（走原签到流程，由后续步骤给出失效提示）。
    """
    if raw_cookie:
        csrf, _checked_in, _is_login = fetch_checkin_state(raw_cookie)
        if csrf is not None:
            return raw_cookie
        if creds:
            print("[linux.sb] Cookie 已失效，尝试用账号密码重新登录")
            new_cookie = browser_login(creds)
            print("[linux.sb] 账号密码登录成功，已取得新 Cookie")
            return new_cookie
        return raw_cookie

    if creds:
        print("[linux.sb] 未配置 LINUXSB_COOKIE，使用账号密码登录")
        return browser_login(creds)
    return None


def fetch_checkin_state(cookie):
    """
    访问签到页，返回 (csrf_token, 是否已签到, 是否为登录页)。

    注意区分「签到页」与「登录页」：未登录访问 /daily_checkin 会被 302 到
    /login，而登录页的登录表单同样带 name="_csrf" 隐藏字段——若把登录页的
    CSRF 当作有效凭据，会在未登录状态下提交出「假签到成功」。因此 URL 落在
    /login 或页面含密码输入框时视为 cookie 失效，csrf 返回 None。
    """
    response = requests.get(
        CHECKIN_URL, headers=PAGE_HEADERS, cookies=parse_cookies(cookie), timeout=30
    )
    if response.status_code != 200:
        raise RuntimeError(f"获取签到页面失败，HTTP {response.status_code}")

    html = response.text
    final_url = getattr(response, "url", None) or CHECKIN_URL
    # 登录页特征：最终 URL 是 /login，或 HTML 含登录表单的密码输入框
    if "/login" in final_url or 'name="password"' in html:
        return None, CHECKED_IN_TEXT in html, True

    match = CSRF_RE.search(html)
    csrf = match.group(1) if match else None
    checked_in = CHECKED_IN_TEXT in html
    return csrf, checked_in, False


def send_checkin_request(cookie, csrf):
    """执行签到 POST 请求，返回完整 Response（调用方负责解析 JSON）。"""
    response = requests.post(
        CHECKIN_URL,
        headers=POST_HEADERS,
        cookies=parse_cookies(cookie),
        data={"_csrf": csrf},
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(f"签到请求失败，HTTP {response.status_code}")
    return response


def merge_response_cookies(cookie_str, response):
    """
    把签到 POST 响应中新签发的 cookie（服务端可能在签到/登录时轮换会话）
    合并进原 cookie 字符串，供后续概览 GET 使用，避免旧会话失效被踢回登录页。
    """
    new_cookies = getattr(response, "cookies", None)
    if not new_cookies:
        return cookie_str
    merged = parse_cookies(cookie_str)
    for name, value in new_cookies.items():
        merged[name] = value
    updated = "; ".join(f"{k}={v}" for k, v in merged.items())
    if updated != cookie_str:
        print("[linux.sb] 服务端轮换了会话 cookie，已合并用于概览获取")
    return updated


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
    调试辅助：打印「当前积分」附近的页面片段（已脱敏），便于按实际页面结构
    调整用户名/积分解析正则。LINUXSB_DEBUG=1 时输出；概览提取全部落空时自动输出。
    """
    def redact(segment):
        segment = re.sub(r'name="_csrf"\s+value="[^"]*"', 'name="_csrf" value="***"', segment)
        # 用户名等个人字段打码，避免落入公开仓库日志
        segment = re.sub(r'(user-name[^>]*>)[^<]+', r'\1***', segment)
        segment = re.sub(r'(href="/user/\d+"[^>]*>)[^<]+', r'\1***', segment)
        return segment

    segment = None
    for keyword in ("当前积分", "积分", "签到"):
        pos = html.find(keyword)
        if pos != -1:
            start = max(0, pos - 250)
            segment = html[start:pos + 120]
            break
    if segment is None:
        segment = html[:700]
    print(f"[linux.sb][debug] 页面片段：\n{redact(segment)}")


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
    csrf, checked_in, is_login_page = fetch_checkin_state(cookie)

    if csrf is None:
        # 仅当页面是真正的签到页（模板不再渲染 _csrf 字段）时才回退到
        # cookie 中的 bbs_csrf；登录页说明 cookie 已失效，绝不兜底（否则假签到）
        if not is_login_page:
            page_csrf = (parse_cookies(cookie) or {}).get("bbs_csrf")
            if page_csrf:
                print("[linux.sb] 签到页未渲染 _csrf 字段，改用 cookie 中的 bbs_csrf 签到")
                csrf = page_csrf
        if csrf is None:
            return False, "Cookie 已失效或页面结构变化：未找到 CSRF token，请重新登录 linux.sb 并更新 LINUXSB_COOKIE", None

    if checked_in:
        summary, username = _build_summary(["签到结果: 今日已签到，无需重复签到"], cookie)
        return True, summary, username

    response = send_checkin_request(cookie, csrf)
    # 服务端可能在签到响应中轮换会话 cookie，先合并再用后续请求
    cookie = merge_response_cookies(cookie, response)
    result = response.json()
    if result.get("ok") in (1, True, "1", "true"):
        message = result.get("message", "")
        # POST 响应中 ok/message 之外的字段一并展示（不同站点版本字段名不同）；
        # redirect 是服务端「签到后跳回签到页」的固定路径，无信息量，不展示
        extras = [f"{key}: {value}" for key, value in result.items()
                  if key not in ("ok", "message", "redirect")]
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
    status = None
    final_url = None
    try:
        response = requests.get(
            CHECKIN_URL, headers=PAGE_HEADERS, cookies=parse_cookies(cookie), timeout=30
        )
        status = response.status_code
        # 跟随重定向后的最终 URL（requests.Response 自带，mock 场景可能缺失）
        final_url = getattr(response, "url", None)
        if status == 200:
            html = response.text
    except requests.RequestException as exc:
        print(f"[linux.sb] 概览页 GET 异常：{exc}")

    username = None
    if html:
        # 每次运行输出一行页面概要（无敏感信息），便于核对登录态页面形态
        title_match = re.search(r"<title>([^<]*)</title>", html)
        title = title_match.group(1).strip() if title_match else "?"
        print(f"[linux.sb] 概览页: HTTP {status}，URL {final_url}，标题「{title}」，长度 {len(html)}")
        username = extract_username(html)
        meta = extract_checkin_meta(html)
        for label, value in meta:
            lines.append(f"{label}: {value}")
        # 提取全部落空时自动输出脱敏片段，方便直接定位页面结构
        if not meta:
            _debug_dump_checkin_area(html)
        elif os.getenv("LINUXSB_DEBUG", "") == "1":
            _debug_dump_checkin_area(html)
    else:
        # 拿不到概览时把真实原因写进日志，方便定位（公开日志不含敏感信息）
        location = f"，最终 URL {final_url}" if final_url else ""
        print(f"[linux.sb] 概览页未取到：HTTP {status}{location}")
    # 抓不到概览信息时至少注明原因，避免通知里只有孤零零一行结果
    if len(lines) == 1:
        lines.append("概览信息: 未从页面取到（模板差异或 Cookie 权限不足）")
    return "\n".join(lines), username


def run():
    """
    执行 linux.sb 每日签到并推送通知，返回进程退出码（全部成功为 0，否则为 1）。
    登录策略：Cookie 优先（多账号用 & 分隔依次签到）；Cookie 缺失或失效时，
    若配置了 LINUXSB_ACCOUNT 则自动用浏览器登录兜底（最多补一次登录）。
    单个账号失败不中断其余账号。通知格式对齐 nodeseek_daily：
    每个账号一段，段首带「签到时间」与概览行。
    """
    raw_cookies = os.getenv("LINUXSB_COOKIE", "").strip()
    creds = load_account_creds()
    if not raw_cookies and not creds:
        # 与 DEEPFLOOD_COOKIE 一致：未配置即视为未启用该站，静默跳过、不算失败
        print("[linux.sb] 未配置 LINUXSB_COOKIE 且未配置 LINUXSB_ACCOUNT，跳过 linux.sb 签到")
        return 0

    # 签到前随机延迟，拉开与 NodeSeek 等站的执行时间间隔
    gap = random.randint(SITE_GAP_MIN, SITE_GAP_MAX)
    print(f"[linux.sb] 等待 {gap} 秒后再开始，避免连续签到被风控")
    time.sleep(gap)

    cookie_list = raw_cookies.split("&") if raw_cookies else [None]
    print(f"[linux.sb] 共 {len(cookie_list)} 个账号开始签到（Cookie 优先，必要时账号密码兜底）")

    results = []
    sections = []
    login_used = False  # 凭据登录最多补一次，多账号同时失效时只救第一个
    for idx, cookie in enumerate(cookie_list, start=1):
        # 记录本账号开始签到的时间，写入通知，便于核对执行时点
        started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            effective = cookie
            if effective is None:
                # 未配置 cookie：直接用账号密码登录（凭据只补登录一次）
                if creds and not login_used:
                    effective = ensure_valid_cookie(None, creds)
                    login_used = True
            elif fetch_checkin_state(effective)[0] is None:
                # cookie 失效：有凭据则登录替换一次，否则保留原值走失效报错
                if creds and not login_used:
                    effective = ensure_valid_cookie(effective, creds)
                    login_used = True
            success, summary, username = sign_in_account(effective)
        except Exception as error:  # 网络异常、登录失败等：单账号失败不影响其余账号
            success, summary, username = False, f"签到异常：{error}", None
        # 账号标识优先用页面解析的用户名，取不到才显示「账号 N」；绝不使用 cookie 内容。
        # 用户名只进通知，不出现在日志（日志暴露在公开仓库的 Actions 页面）
        display = username or f"账号 {idx}"
        print(f"[linux.sb] 账号 {idx}：{summary}")
        results.append((success, summary))
        sections.append(
            f"{display}\n"
            f"签到时间: {started_at}\n"
            f"{summary}"
        )

    all_success = all(success for success, _ in results)
    title = "LinuxSB 每日任务" + ("" if all_success else "（签到异常）")
    # 通知不输出站点域名与任务开始时间：linux.sb 只有一站，任务的开始时间
    # 与本账号的签到时间语义重叠且相差随机延迟，只保留账号级「签到时间」
    content = "\n\n".join(sections)
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