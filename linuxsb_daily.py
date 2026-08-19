# -- coding: utf-8 --
"""
linux.sb（烧饼社区）每日自动签到脚本。

与 nodeseek_daily.py 同属本仓库的多站签到体系：NodeSeek、DeepFlood 与 linux.sb
可顺序执行，共用 notify.py 推送通知。

签到有两条通道，按站点当前的防护状态自动选择：
1. requests 通道（快，站点未启用挑战时使用）。接口协议对照 qd-today/templates
   的 Linux_SB.har 模板确认：
   - GET  /daily_checkin 携带 Cookie，从 HTML 取 <input name="_csrf"> 与
     「今日已签到」状态
   - POST /daily_checkin 携带 Cookie 与表单 _csrf，响应 {"ok":1,...} 为成功
2. 浏览器通道（过 Cloudflare 用）。2026-08 起 linux.sb 会间歇性开启 Cloudflare
   托管挑战：命中时 requests 通道收到 HTTP 403 + Cf-Mitigated: challenge +
   「Just a moment...」挑战页。挑战不是常开的（实测同一出口 IP 相隔约两小时
   一次 403、一次 200），但一旦命中，纯 HTTP 客户端无解——requests 与
   curl_cffi 的 chrome/edge/firefox 指纹伪装全部 403，与出口 IP、请求头是否
   完整无关，只能由真实浏览器放行。策略与 nodeseek_daily.py 同源：
   undetected-chromedriver 有头模式 + xvfb 虚拟显示，等挑战自动通过后注入
   Cookie，在同一浏览器会话内用页面 fetch 完成签到。

环境变量：
- LINUXSB_COOKIE：登录 Cookie；多账号用 & 分隔，依次签到、单账号失败不中断。
  优先使用；失效或未配置时，若提供 LINUXSB_ACCOUNT 则自动用账号密码登录
- LINUXSB_ACCOUNT：账号密码凭据（JSON：{"username":"...","password":"..."}），
  作为 Cookie 的兜底登录方式。Cookie 有效时不使用；Cookie 失效或缺失时
  用浏览器自动登录（算术题验证码自动解析，PoW 由页面自身 JS 完成）
- SITE_GAP_MIN / SITE_GAP_MAX：签到前随机延迟范围（秒，默认 60-180），
  与 nodeseek_daily.py 的站间延迟共用同一对变量，降低被风控判为批量行为的概率
- LINUXSB_FORCE_BROWSER：置 1 时跳过 requests 探测直接走浏览器通道。用于站点
  长期开盾时省掉必然失败的探测，或在挑战未触发的时段验证浏览器通道
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

# Cloudflare 挑战页（"Just a moment..." 托管挑战）的页面特征，命中任一即说明
# 拿到的不是站点正文。与 nodeseek_daily.py 的 CF_CHALLENGE_MARKERS 同源。
CF_CHALLENGE_MARKERS = ("just a moment", "challenges.cloudflare.com", "cf-browser-verification")

# 不注入浏览器的 cookie：Cloudflare 的 cf_clearance / __cf_bm 与签发时的出口 IP
# 和 User-Agent 绑定，在 Actions 这类异地环境注入对不上的旧值，比不带更容易被
# 判为异常（浏览器过挑战后会自行签发本机可用的新值）；_ga 等统计 cookie 与登录
# 态无关。前缀匹配以覆盖 _ga_XXXX 这类带后缀的变体。与 nodeseek_daily.py 同源。
SKIP_COOKIE_PREFIXES = ("cf_clearance", "__cf_bm", "__cflb", "_ga", "_gid", "_gat")


class CloudflareChallenged(RuntimeError):
    """
    requests 通道被 Cloudflare 挑战拦截（HTTP 403 + Cf-Mitigated: challenge）。

    单独立类型而不复用 RuntimeError，是为了让 run() 能把「站点开了挑战」与
    「网络异常/页面结构变化」区分开：前者要切换到浏览器通道重试，后者才算失败。
    """


class CookieExpired(RuntimeError):
    """浏览器通道注入 Cookie 后仍未取得登录态，说明 Cookie 本身已失效。"""


def should_skip_cookie(name):
    """判断某个 cookie 是否应跳过注入浏览器（大小写不敏感的前缀匹配）。"""
    lowered = (name or "").strip().lower()
    return any(lowered.startswith(prefix) for prefix in SKIP_COOKIE_PREFIXES)


def header_value(response, name, default=""):
    """
    大小写不敏感地读取响应头。

    requests 的 Response.headers 本就是 CaseInsensitiveDict，但浏览器通道与测试
    里传入的可能是普通 dict（键名大小写按抓包原样），统一在此规整，避免调用方
    因为大小写猜错而漏读 cf-mitigated 这类关键判据。
    """
    headers = getattr(response, "headers", None) or {}
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target:
            return str(value).strip()
    return default


def is_cf_challenge_response(response):
    """
    判断 requests 响应是否为 Cloudflare 挑战页。

    两个独立证据任一命中即认定：响应头 Cf-Mitigated: challenge（Cloudflare 明确
    标注本次拦截为挑战），或响应体开头含挑战页特征串。只看响应体开头即可——
    挑战页体积仅几 KB，且特征都在 head 内。
    """
    if header_value(response, "cf-mitigated").lower() == "challenge":
        return True
    head = (getattr(response, "text", "") or "")[:3000].lower()
    return any(marker in head for marker in CF_CHALLENGE_MARKERS)



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


class CheckinRejected(RuntimeError):
    """服务端明确拒绝签到（ok:0 且非「已签到」类文案），属业务失败而非流程异常。"""


class _Stage:
    """
    记录浏览器流程当前所处阶段，失败时写进异常信息便于定位是哪一步挂的。
    浏览器流程跨多个函数（登录/注入 Cookie，再就地签到），用可变对象传递阶段
    比在每层返回值里透传字符串更简洁。
    """

    def __init__(self, name):
        self.name = name

    def set(self, name):
        self.name = name


def create_driver():
    """
    构建 undetected-chromedriver 实例，与 nodeseek_daily.py 的 create_driver 同
    配比：默认有头 + 外部 xvfb 提供虚拟显示，始终降低自动化特征、不覆盖真实 UA
    （伪造 UA 与真实平台/版本不一致反而会成为 Cloudflare 的识别特征）。

    默认有头而非无头的两个原因：--headless=new 与 workflow 的 xvfb-run 虚拟显示
    叠加会让 UCD 导航后 find_element 卡到超时（DOM 已完整渲染却定位不到元素）；
    且无头指纹更容易被 Cloudflare 拦在挑战页。纯本地无显示环境调试可设 HEADLESS=true。

    浏览器库在函数内局部导入，保证未安装浏览器依赖时 requests 通道仍可用。
    """
    import undetected_chromedriver as uc

    options = uc.ChromeOptions()
    if env_bool("HEADLESS", False):
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
    else:
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")

    # runner 预装的 Chrome 往往落后于最新 chromedriver，版本不匹配会直接抛
    # SessionNotCreatedException；显式传入实际大版本号强制取匹配的驱动
    driver_kwargs = {}
    version_main = detect_chrome_major_version()
    if version_main:
        print(f"[linux.sb] 检测到 Chrome 大版本 {version_main}，使用匹配的驱动")
        driver_kwargs["version_main"] = version_main
    return uc.Chrome(options=options, **driver_kwargs)


def is_cloudflare_challenge(driver):
    """判断浏览器当前是否停在 Cloudflare 挑战页（与 nodeseek_daily.py 同源）。"""
    try:
        title = (driver.title or "").lower()
        if any(marker in title for marker in CF_CHALLENGE_MARKERS):
            return True
        # 挑战页体积很小且特征都在 head 内，截取开头即可判断
        head = (driver.page_source or "")[:3000].lower()
        return any(marker in head for marker in CF_CHALLENGE_MARKERS)
    except Exception as exc:
        print(f"[linux.sb] 检测 Cloudflare 挑战页失败：{exc}")
        return False


def wait_for_cloudflare(driver, timeout=60):
    """
    等待 Cloudflare 挑战自行通过，返回是否已离开挑战页。
    undetected-chromedriver 通常能自动过盾但需要时间；超时返回 False，
    由调用方决定是就此失败还是继续往下试。
    """
    if not is_cloudflare_challenge(driver):
        return True

    print(f"[linux.sb] 检测到 Cloudflare 挑战页，最多等待 {timeout} 秒…")
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(3)
        if not is_cloudflare_challenge(driver):
            print("[linux.sb] Cloudflare 挑战已通过")
            return True

    print("[linux.sb] Cloudflare 挑战在超时内未通过")
    return False


def wait_dom_ready(driver, selector, timeout=30, required=True):
    """
    轮询 DOM 中是否真实存在指定选择器，命中返回 True。

    不用 WebDriverWait + EC.presence_of_element_located：undetected-chromedriver
    在 xvfb 有头环境偶发「导航后 find_element 卡死」——等满 30 秒拿不到、异常
    Message 为空，重开浏览器仍卡在同一处。execute_script 直接查 DOM 树可绕过
    UCD 对元素可见性/状态的判定坑。

    required=True（如登录表单）超时即抛异常，因为后续操作离了该元素做不下去；
    required=False（如签到页形态核对）只打警告返回 False——签到本身由页面内
    fetch 完成、不依赖任何元素，把形态核对做成硬失败只会在站点改版时凭空多出
    一条失败路径。
    """
    end = time.time() + timeout
    while time.time() < end:
        if driver.execute_script("return !!document.querySelector(arguments[0])", selector):
            return True
        time.sleep(0.5)
    if required:
        raise RuntimeError(f"页面元素 {selector} 在 {timeout} 秒内未出现在 DOM")
    print(f"[linux.sb] 警告：{timeout} 秒内未见 {selector}（模板可能已改版），"
          f"仍按流程继续签到")
    return False


# 签到页加载完成的稳定标志：签到卡片、摘要面板或用户卡，登录态下必居其一。
# 三个类名均已在站点样式表中核实存在（.daily-checkin-wrap / .daily-checkin-head、
# .daily-checkin-page-panel .admin-list-head、.user-card）。
# 不能等 input[name="_csrf"]——该隐藏字段只在登录页/未登录态渲染，登录后的签到页
# 不含它，等它必然超时。
CHECKIN_PAGE_READY_SELECTOR = ".daily-checkin-wrap, .daily-checkin-page-panel, .user-card"
# 等签到页形态的上限（秒）。只是核对页面是否如预期渲染，超时不算失败，故不必等太久
CHECKIN_PAGE_READY_TIMEOUT = 20


def _checkin_in_browser(driver, stage, fallback_csrf=None):
    """
    在已具备登录态的浏览器会话内完成签到，返回 (结果行列表, 用户名或 None)。

    Cookie 注入与账号密码登录两条浏览器通道在此汇合：二者到此都是「已登录的
    浏览器会话」，之后的签到与概览提取完全一致。签到用页面内 fetch 发起，不把
    cookie 导出到 requests——登录态天然一致（此前多次尝试 cookie 导出到 requests
    均无法复现登录态，故放弃该路线）。

    fallback_csrf：现场读不到 bbs_csrf cookie 时使用的备用令牌（账号密码通道传
    登录表单里的 _csrf）。签到失败/登录态无效分别抛 CheckinRejected/CookieExpired。
    """
    stage.set("访问签到页")
    driver.get(CHECKIN_URL)
    stage.set("等待签到页通过 Cloudflare")
    wait_for_cloudflare(driver)
    # 先判 URL 再等 DOM：未登录访问 /daily_checkin 会被 302 到 /login（实测），
    # 登录页永远不会出现签到页元素。顺序反了会白等满超时，且异常退化成普通
    # RuntimeError——run() 就识别不出「Cookie 失效」、走不到账号密码兜底。
    if "/login" in driver.current_url:
        raise CookieExpired("登录态未生效：签到页被重定向到登录页")
    stage.set("等待签到页加载")
    wait_dom_ready(driver, CHECKIN_PAGE_READY_SELECTOR,
                   timeout=CHECKIN_PAGE_READY_TIMEOUT, required=False)

    lines = []
    bonus_from_response = None
    if CHECKED_IN_TEXT in driver.page_source:
        lines.append("签到结果: 今日已签到，无需重复签到")
    else:
        stage.set("执行签到请求（页面内 fetch）")
        driver.set_script_timeout(20)
        # 签到 CSRF 必须等于当前请求 Cookie 里的 bbs_csrf 值（服务端按两者一致
        # 校验，与 requests 通道 sign_in_account 同一逻辑）。登录成功后服务端会把
        # bbs_csrf 删掉重发、签到页也不再渲染 _csrf 字段，所以从当前浏览器 cookie
        # 现场读 bbs_csrf 作为 _csrf 提交，保证提交值与请求携带的 cookie 一致
        # （浏览器会话自动随 fetch 带上 cookie）。
        bbs_csrf_cookie = next(
            (c["value"] for c in driver.get_cookies() if c["name"] == "bbs_csrf"), ""
        )
        if bbs_csrf_cookie and bbs_csrf_cookie not in ("deleted", "archived"):
            csrf_to_submit = bbs_csrf_cookie
        elif fallback_csrf:
            # HttpOnly 时 get_cookies 可能读不到，或值被标记失效——回退备用令牌
            print(f"[linux.sb] 警告：bbs_csrf cookie 未取到（{bbs_csrf_cookie!r}），"
                  f"回退用备用 _csrf")
            csrf_to_submit = fallback_csrf
        else:
            raise CookieExpired(
                f"未取到 bbs_csrf cookie（{bbs_csrf_cookie!r}）且无备用令牌，"
                "Cookie 可能已失效"
            )
        result = driver.execute_async_script(
            "const done = arguments[arguments.length - 1];"
            "const csrf = arguments[0];"
            "fetch('/daily_checkin', {"
            "  method: 'POST',"
            "  headers: {"
            "    'Content-Type': 'application/x-www-form-urlencoded',"
            "    'X-Requested-With': 'XMLHttpRequest'"
            "  },"
            "  body: '_csrf=' + encodeURIComponent(csrf)"
            "}).then(r => r.json()).then(done).catch(e => done({ok: 0, message: String(e)}));",
            csrf_to_submit,
        )
        message = (result or {}).get("message", "") or ""
        if (result or {}).get("ok") in (1, True, "1", "true"):
            lines.append(f"签到结果: 签到成功{f'（{message}）' if message else ''}")
            # 尝试从签到 POST 响应里取“本次签到获得的积分数”。字段名随站点版本
            # 而异（bonus/points/获得积分 等），用 _checkin_bonus 统一兜底提取；
            # 响应里没有就等刷新签到页后从 toast 文案补（两个来源总有一个命中）。
            bonus_from_response = _checkin_bonus(result)
            if bonus_from_response is not None:
                lines.append(f"本次签到获得: {bonus_from_response} 积分")
        elif any(word in message for word in ("已签到", "已打卡", "重复签到")):
            lines.append(f"签到结果: 今日已签到，无需重复签到（服务端：{message}）")
        else:
            hint = "（Cookie 可能已失效，请重新登录 linux.sb）" if "过期" in message else ""
            raise CheckinRejected(f"签到失败：{message}{hint}")

    # 刷新签到页提取用户名/积分/连续签到概览。签到已经完成，这一步只为补充展示
    # 信息，同样不因页面形态对不上而失败（required=False）
    stage.set("刷新签到页提取概览")
    driver.refresh()
    wait_for_cloudflare(driver)
    wait_dom_ready(driver, CHECKIN_PAGE_READY_SELECTOR,
                   timeout=CHECKIN_PAGE_READY_TIMEOUT, required=False)
    print(f"[linux.sb] 签到页：URL {driver.current_url}，标题「{driver.title}」")
    html = driver.page_source
    meta = extract_checkin_meta(html)
    for label, value in meta:
        lines.append(f"{label}: {value}")
    # 签到 POST 响应没给积分时，从签到页 toast/文案（__pageFlash「签到成功，
    # 连续 N 天，获得 M 积分」等）补“本次签到获得 N 积分”
    if bonus_from_response is None:
        toast_bonus = _checkin_bonus_from_text(html)
        if toast_bonus is not None:
            lines.append(f"本次签到获得: {toast_bonus} 积分")
    # 概览提取落空时输出脱敏页面片段，便于按真实 DOM 结构校准提取
    if not meta:
        _debug_dump_checkin_area(html)
    if len(lines) == 1:
        lines.append("概览信息: 未从页面取到（模板差异或页面权限不足）")
    print(f"[linux.sb] 浏览器签到完成：{lines[0]}")
    return lines, extract_username(html)


def _browser_failure(driver, stage, exc, action):
    """
    浏览器流程异常时采集脱敏页面快照并包装异常信息，便于定位卡在哪一步。
    快照只打印 body 片段且已脱敏，可安全出现在公开仓库的 Actions 日志。
    """
    try:
        live_url = driver.current_url
        live_src = driver.page_source or ""
    except Exception:
        live_url, live_src = "?", ""
    print(f"[linux.sb] 失败时页面：URL {live_url}，长度 {len(live_src)}")
    if live_src:
        _debug_dump_checkin_area(live_src)
    return RuntimeError(f"浏览器{action}失败（{stage.name}、URL {live_url}）：{exc}")


def browser_sign_in_with_cookie(cookie):
    """
    Cookie 注入浏览器并就地签到，返回 (成功与否, 结果摘要, 用户名或 None)。

    这是挑战命中时 Cookie 通道的唯一可行路径：挑战要求执行页面 JS，
    requests / curl_cffi 均被 403，只有真实浏览器能放行。流程与
    nodeseek_daily.py 的 inject_site_cookies 同源——先访问首页让 UCD 过挑战、
    拿到本机可用的 cf_clearance，再注入登录 cookie，最后打开签到页签到。

    注入时跳过 cf_clearance / __cf_bm 等与出口 IP + UA 绑定的 cookie（见
    SKIP_COOKIE_PREFIXES）：Actions 的出口 IP 与用户复制 cookie 时不同，注入
    对不上的旧值比不带更容易被判为异常。
    """
    driver = create_driver()
    stage = _Stage("打开首页过 Cloudflare")
    try:
        driver.get(BASE_URL)
        if not wait_for_cloudflare(driver):
            raise RuntimeError("首页未通过 Cloudflare 挑战，无法注入 Cookie")

        stage.set("注入 Cookie")
        injected = []
        skipped = []
        cookie_items = parse_cookies(cookie)
        # 配置里自带的 bbs_csrf 作为备用令牌：正常情况下签到时从浏览器现场读取
        # 即可，这里只覆盖「服务端换发了新值又读不回来」的极端情况
        fallback_csrf = cookie_items.get("bbs_csrf") or None
        for name, value in cookie_items.items():
            if should_skip_cookie(name):
                skipped.append(name)
                continue
            try:
                # domain 用带前导点的形式覆盖子域，与 nodeseek 注入策略一致
                driver.add_cookie({"name": name, "value": value,
                                   "domain": ".linux.sb", "path": "/"})
                injected.append(name)
            except Exception as exc:
                print(f"[linux.sb] 注入 cookie {name} 失败：{exc}")
        # 只打印名称不打印值，便于核对配置完整性而不泄漏凭据
        print(f"[linux.sb] 注入 {len(injected)} 个 cookie：{injected}"
              + (f"，跳过 IP/UA 绑定项：{skipped}" if skipped else ""))
        if not injected:
            raise CookieExpired("没有任何有效 cookie 可注入，请检查 LINUXSB_COOKIE 格式")
        if "bbs_auth" not in injected:
            # bbs_auth 是该论坛的登录态所在，缺失时后续必然停在未登录页
            print("[linux.sb] 警告：未注入 bbs_auth，登录态很可能不完整")

        lines, username = _checkin_in_browser(driver, stage, fallback_csrf=fallback_csrf)
        return True, "\n".join(lines), username
    except CheckinRejected as exc:
        # 服务端明确拒绝（如令牌过期）不是浏览器抽风，重试无意义，如实上报即可
        # （与 browser_sign_in 同一处理，避免白白重开两次浏览器）
        return False, str(exc), None
    except CookieExpired:
        # Cookie 本身失效：交给 run() 决定是否转账号密码兜底，不在此吞掉
        raise
    except Exception as exc:
        raise _browser_failure(driver, stage, exc, "Cookie 签到") from exc
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def browser_sign_in(creds):
    """
    账号密码兜底登录并就地签到，返回 (成功与否, 结果摘要, 用户名或 None)。

    与 requests 签到路径的区别：登录成功后在同一个浏览器会话内直接访问
    签到页，并用页面内 fetch 发起签到 POST——不经过 cookie 导出/拼接/重发
    环节，登录态天然一致（此前多次尝试 cookie 导出到 requests 均无法复现
    登录态，故放弃该路线）。算术题验证码由脚本解析填写，PoW 由页面 JS 计算。
    浏览器库（selenium/undetected_chromedriver 等）函数内局部导入，保证未
    安装浏览器依赖时纯 requests 签到仍可正常使用。
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait

    driver = create_driver()
    stage = _Stage("打开登录页")
    try:
        driver.get(f"{BASE_URL}/login")
        # 站点已启用 Cloudflare 托管挑战，登录页同样先落在挑战页，需等其放行
        stage.set("等待登录页通过 Cloudflare")
        if not wait_for_cloudflare(driver):
            raise RuntimeError("登录页未通过 Cloudflare 挑战")
        wait = WebDriverWait(driver, 30)

        stage.set("填写登录表单")
        wait_dom_ready(driver, "[name=username]")
        username_input = driver.find_element(By.NAME, "username")
        username_input.clear()
        username_input.send_keys(creds["username"])
        password_input = driver.find_element(By.NAME, "password")
        password_input.clear()
        password_input.send_keys(creds["password"])

        # 读取算术题题面并填入计算结果（例如「9 × 4 = ?」-> 36）
        # 同样用 DOM 轮询（见 wait_dom_ready 的理由），避免 UCD 可见性判定坑
        wait_dom_ready(driver, ".native-captcha-question")
        question_el = driver.find_element(By.CLASS_NAME, "native-captcha-question")
        answer = solve_captcha_question(question_el.text)
        wait_dom_ready(driver, "[name=native_captcha_answer]")
        answer_input = driver.find_element(By.NAME, "native_captcha_answer")
        answer_input.clear()
        answer_input.send_keys(answer)

        # 蜜罐字段 native_captcha_company 保持为空（伪装真人必须留空）
        # 提交按钮必须从密码输入框所在的登录表单内找：登录页顶部还有
        # .search-form（搜索框，action=/index.php），用全局 form button
        # 选择器会匹配到搜索按钮——曾因此点击了搜索而非登录，跳到空搜索页
        # （index.php?field=title&q=）且被误判为登录成功
        login_form = password_input.find_element(By.XPATH, "ancestor::form[1]")
        # 登录前记住登录表单的 _csrf：登录成功后服务端会删除 bbs_csrf cookie，
        # 签到页也不再渲染 _csrf 隐藏字段——而签到 POST 校验的正是本会话的 CSRF
        # 令牌（与 Cookie 路径一致：sign_in_account 用 bbs_csrf cookie 值即本会话
        # CSRF）。所以这里把登录时的 _csrf 存下来，留给签到 fetch 复用。
        login_csrf = login_form.find_element(By.NAME, "_csrf").get_attribute("value")
        if not login_csrf:
            raise RuntimeError("登录表单未取到 _csrf，无法为签到准备 CSRF 令牌")
        submit = login_form.find_element(By.CSS_SELECTOR, "button[type=submit], button")
        submit.click()
        # 登录成功的可靠特征：登录表单（password 输入框）从页面消失。
        # 该站登录表单只在 /login 呈现，登录成功后其他页面不再有密码输入框；
        # 登录失败停留在 /login（表单恒在）会在此超时并明确报错
        wait.until(lambda d: 'name="password"' not in (d.page_source or ""))
        print(f"[linux.sb] 账号密码登录成功（URL {driver.current_url}），开始签到")

        # 以同一浏览器会话访问签到页并执行签到（与 Cookie 通道共用同一段逻辑）。
        # 登录表单那个 _csrf 作为备用令牌传入：登录成功后服务端会把 bbs_csrf 删掉
        # 重发，现场读不到时用它兜底。
        lines, username = _checkin_in_browser(driver, stage, fallback_csrf=login_csrf)
        return True, "\n".join(lines), username
    except CheckinRejected as exc:
        # 服务端明确拒绝签到属业务结果，按失败上报即可，不必重开浏览器重试
        return False, str(exc), None
    except Exception as exc:
        raise _browser_failure(driver, stage, exc, "登录/签到") from exc
    finally:
        try:
            driver.quit()
        except Exception:
            pass


# 浏览器登录偶发卡死时的最大重试次数。undetected-chromedriver 在 Actions runner
# 无头/有头环境偶发导航后 find_element 卡到超时（曾完整成功过一次，证明链路
# 本身可行，只是稳定性受 runner 影响）。卡死时 driver.quit 重开浏览器重试，
# 最多 2 次仍失败才放弃，避免单次偶发抽风让整天签到失败。
_BROWSER_LOGIN_MAX_ATTEMPTS = 2


def _browser_with_retry(action, func, arg, max_attempts=_BROWSER_LOGIN_MAX_ATTEMPTS):
    """
    带重试地执行一次浏览器签到动作：单次卡死/失败时重开浏览器再来，最多
    max_attempts 次。返回 func 的 (成功与否, 结果摘要, 用户名或 None)。

    CookieExpired 不重试直接上抛：Cookie 失效重开浏览器也不会变有效，
    由调用方转去账号密码兜底登录才有意义。
    每次尝试打印动作名与序号，便于从日志分辨是「一次都没成」还是「第 N 次才成」。
    """
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            if attempt > 1:
                print(f"[linux.sb] 浏览器{action}第 {attempt} 次重试（共 {max_attempts} 次）")
            return func(arg)
        except CookieExpired:
            raise
        except Exception as exc:
            last_error = exc
            print(f"[linux.sb] 浏览器{action}第 {attempt} 次尝试失败：{exc}")
            if attempt < max_attempts:
                time.sleep(5)  # 重试前短暂停顿，避免紧挨着重启浏览器被风控
    raise RuntimeError(f"浏览器{action}连续 {max_attempts} 次失败：{last_error}") from last_error


def browser_sign_in_with_retry(creds, max_attempts=_BROWSER_LOGIN_MAX_ATTEMPTS):
    """带重试的账号密码浏览器登录签到。"""
    return _browser_with_retry("登录", browser_sign_in, creds, max_attempts)


def browser_cookie_sign_in_with_retry(cookie, max_attempts=_BROWSER_LOGIN_MAX_ATTEMPTS):
    """带重试的 Cookie 注入浏览器签到（Cloudflare 挑战下 Cookie 通道的走法）。"""
    return _browser_with_retry("Cookie 签到", browser_sign_in_with_cookie, cookie, max_attempts)



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





def fetch_checkin_state(cookie):
    """
    访问签到页，返回 (csrf_token, 是否已签到, 是否为登录页)。

    注意区分「签到页」与「登录页」：未登录访问 /daily_checkin 会被 302 到
    /login，而登录页的登录表单同样带 name="_csrf" 隐藏字段——若把登录页的
    CSRF 当作有效凭据，会在未登录状态下提交出「假签到成功」。因此 URL 落在
    /login 或页面含密码输入框时视为 cookie 失效，csrf 返回 None。

    站点开启 Cloudflare 托管挑战时抛 CloudflareChallenged（而非通用异常），
    由调用方切换到浏览器通道；其余非 200 才是真正的请求失败。
    """
    response = requests.get(
        CHECKIN_URL, headers=PAGE_HEADERS, cookies=parse_cookies(cookie), timeout=30
    )
    if response.status_code != 200:
        if is_cf_challenge_response(response):
            # 把 Cloudflare 的判定证据打进日志：只有一行「HTTP 403」时无法分辨
            # 是站点挑战、IP 封禁还是接口变更，排查只能靠猜
            mitigated = header_value(response, "cf-mitigated", "-")
            ray = header_value(response, "cf-ray", "-")
            raise CloudflareChallenged(
                f"Cloudflare 挑战拦截 requests 通道（HTTP {response.status_code}，"
                f"cf-mitigated={mitigated}，cf-ray={ray}）"
            )
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


# “本次签到获得的积分数”的候选字段名（签到 POST 响应 JSON 里随站点版本而异）
_CHECKIN_BONUS_FIELDS = ("bonus", "points", "gain", "reward", "score", "earned")
# 签到页 toast / __pageFlash 文案里“本次签到获得 N 积分”的提取正则。
# 常见文案（bbs1 v8.6.1）：「签到成功，连续 2 天，获得 76 积分」，
# 兼容「获得 xx 积分」「奖励 xx 积分」「积分 +xx」等变体。
_BONUS_TEXT_RE = re.compile(
    r"(?:获得|获得积分|奖励|积分[＋+]\s*|本次签到获得)\s*[＋+]?\s*(\d+)\s*积分"
)


def _checkin_bonus(result):
    """
    从签到 POST 响应 JSON 里提取“本次签到获得的积分数”，返回整数或 None。
    遍历候选字段名，命中数字则返回；无命中返回 None（由 toast 文案补）。
    """
    if not isinstance(result, dict):
        return None
    for key in _CHECKIN_BONUS_FIELDS:
        value = result.get(key)
        if value is None:
            continue
        # 兼容纯数字、字符串数字（可能有 + 前缀，如 "+10"）
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            m = re.search(r"[+\+]?\s*(\d+)", value)
            if m:
                return int(m.group(1))
    return None


def _checkin_bonus_from_text(html):
    """
    从签到页 HTML（toast / __pageFlash 文案）里补“本次签到获得的积分”，
    返回数字字符串或 None。抓不到不报错，不影响签到结果。
    """
    m = _BONUS_TEXT_RE.search(html)
    return m.group(1) if m else None


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
    调试辅助：打印签到页 body 开头片段（已脱敏），便于按实际页面结构
    调整用户名/积分解析正则。LINUXSB_DEBUG=1 时输出；概览提取落空时自动输出。
    """
    def redact(segment):
        segment = re.sub(r'name="_csrf"\s+value="[^"]*"', 'name="_csrf" value="***"', segment)
        # 用户名等个人字段打码，避免落入公开仓库日志
        segment = re.sub(r'(user-name[^>]*>)[^<]+', r'\1***', segment)
        segment = re.sub(r'(href="/user/\d+"[^>]*>)[^<]+', r'\1***', segment)
        return segment

    body = re.search(r"<body[\s\S]*", html)
    segment = (body.group(0) if body else html)[:25000]
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

    通道选择（每个账号独立判定）：
    1. requests 通道：Cookie 有效且站点未开 Cloudflare 挑战时使用，最快
    2. 浏览器 Cookie 通道：requests 被 Cloudflare 挑战拦截（HTTP 403 +
       Cf-Mitigated: challenge）时切换——挑战只有真实浏览器能过
    3. 浏览器账号密码通道：Cookie 本身已失效时，用 LINUXSB_ACCOUNT 兜底登录
       （最多补一次，多账号同时失效时只救第一个）

    置 LINUXSB_FORCE_BROWSER=1 可跳过 requests 探测直接走通道 2。

    单个账号失败不中断其余账号。通知格式对齐 nodeseek_daily：每个账号一段，
    段首带「签到时间」与概览行。
    """
    raw_cookies = os.getenv("LINUXSB_COOKIE", "").strip()
    creds = load_account_creds()
    # 挑战是间歇性的：同一出口 IP 可能上一轮 403、下一轮 200。没有这个开关，
    # 浏览器通道就只能守着站点开盾的时间窗口才验证得到；站点长期开盾时也可用它
    # 省掉每次必然失败的 requests 探测。
    force_browser = os.getenv("LINUXSB_FORCE_BROWSER", "") == "1"
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
    if force_browser:
        print("[linux.sb] LINUXSB_FORCE_BROWSER=1，跳过 requests 探测直接走浏览器通道")

    results = []
    sections = []
    login_used = False  # 凭据登录最多补一次，多账号同时失效时只救第一个
    for idx, cookie in enumerate(cookie_list, start=1):
        # 记录本账号开始签到的时间，写入通知，便于核对执行时点
        started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            # 先探测当前账号 cookie 是否有效：有效直接在 requests 通道签到；被
            # Cloudflare 挑战拦截则转浏览器 Cookie 通道；登录态缺失（302 到
            # /login）则转账号密码登录。仅 LINUXSB_COOKIE 阳性账号探测。
            # 探测与 requests 签到放在同一个 try 内：挑战可能在探测通过之后才
            # 生效（站点中途开盾），两处命中都要走同一条浏览器兜底路径。
            result = None  # requests 通道已出结果时为 (成功, 摘要, 用户名)
            # 与 bool(cookie) 相与：无 Cookie 账号本就该直接走账号密码通道，
            # 强制开关不能把 None 送进浏览器 Cookie 通道
            cf_blocked = force_browser and bool(cookie)
            if cookie and not force_browser:
                try:
                    csrf, _checked_in, is_login = fetch_checkin_state(cookie)
                    if csrf is not None and not is_login:
                        result = sign_in_account(cookie)
                except CloudflareChallenged as challenge:
                    # requests 通道整条不可用（签到 POST 与概览 GET 同样会被拦），
                    # 必须整体改走浏览器，不能只补这一个请求
                    cf_blocked = True
                    print(f"[linux.sb] {challenge}，改用浏览器注入 Cookie 签到")

            if result is not None:
                # Cookie 有效且站点未开挑战：requests 快通道已完成签到
                success, summary, username = result
            elif cf_blocked:
                try:
                    success, summary, username = browser_cookie_sign_in_with_retry(cookie)
                except CookieExpired as expired:
                    # 浏览器已过挑战但仍未登录：Cookie 本身失效，转账号密码兜底
                    print(f"[linux.sb] 浏览器内 Cookie 未取得登录态（{expired}）")
                    if creds and not login_used:
                        print("[linux.sb] 改用账号密码浏览器登录并签到")
                        success, summary, username = browser_sign_in_with_retry(creds)
                        login_used = True
                    else:
                        success, summary, username = False, (
                            f"Cookie 已失效（{expired}），请在浏览器登录 linux.sb "
                            "后复制 Cookie 更新 LINUXSB_COOKIE"
                        ), None
            elif creds and not login_used:
                # Cookie 缺失/失效：用浏览器账号密码登录并就地签到。
                # 纯 requests 复刻登录已证实走不通——服务端的 native_captcha_answer
                # 校验依赖只在服务端持有的哈希盐/算法（token 里 answer 是预签哈希，
                # 与明文答案 sha256 对不上，bbs1 后端未开源），离线无法复刻。
                # 浏览器路径曾完整成功过一次（登录+签到+积分全拿到），只是
                # undetected-chromedriver 在 runner 偶发导航卡死，故加重试兜底。
                # 凭据登录最多补一次，多账号同时失效时只救第一个。
                print("[linux.sb] Cookie 失效，使用账号密码浏览器登录并签到")
                success, summary, username = browser_sign_in_with_retry(creds)
                login_used = True
            else:
                # 无有效 cookie 也无可用的兜底凭据：给出明确失效提示
                success, summary, username = False, (
                    "Cookie 已失效（未配置 LINUXSB_COOKIE 或凭据已用尽），"
                    "请在浏览器登录 linux.sb 后复制 Cookie 更新 LINUXSB_COOKIE"
                ), None
        except Exception as error:  # 网络异常、登录/签到失败等：单账号失败不影响其余账号
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