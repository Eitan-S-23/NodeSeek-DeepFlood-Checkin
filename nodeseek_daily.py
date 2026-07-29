# -- coding: utf-8 --
"""
Copyright (c) 2024 [Hosea]
Licensed under the MIT License.
See LICENSE file in the project root for full license information.
"""
import os
from bs4 import BeautifulSoup
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import random
import re
import shutil
import subprocess
import time
import traceback
import undetected_chromedriver as uc
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains

import notify

# 本地调试时从 .env 读取配置；GitHub Actions 环境直接使用注入的环境变量。
# python-dotenv 缺失时静默跳过，保证已有部署无需改动即可运行。
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

def env_bool(name, default=False):
    """
    解析布尔型环境变量，接受 true/1/yes/on/y（大小写不敏感）为真，其余为假。
    未设置或为空时返回 default。
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("true", "1", "yes", "on", "y")


# 不应注入的 cookie。
# Cloudflare 的 cf_clearance / __cf_bm 与获取时的出口 IP 和 User-Agent 绑定，
# 在 GitHub Actions 这类异地环境注入对不上的值，比不带更容易被判定为异常；
# _ga 等统计 cookie 与登录态无关。前缀匹配以覆盖 _ga_XXXX 这类带后缀的变体。
SKIP_COOKIE_PREFIXES = ("cf_clearance", "__cf_bm", "__cflb", "_ga", "_gid", "_gat")


def should_skip_cookie(name):
    """判断某个 cookie 是否应跳过注入（大小写不敏感的前缀匹配）。"""
    lowered = name.strip().lower()
    return any(lowered.startswith(prefix) for prefix in SKIP_COOKIE_PREFIXES)


def parse_cookie_string(raw):
    """
    解析 NS_COOKIE 字符串，返回 (待注入的 (name, value) 列表, 跳过原因列表)。

    只在名称合法的分号处切分：cookie 值本身可能含分号（例如被截断的 JSON），
    若无条件按分号切分会把一个 cookie 拆成两半，产生不含 = 号的残缺片段。
    因此逐段判断——某段不含 = 号或等号左侧不像合法 cookie 名时，
    视为上一个 cookie 值的延续并拼回去。
    同时把换行当作分隔符，便于 secret 多行粘贴。

    跳过原因中不含 cookie 值，可安全打印到 CI 日志。
    """
    pairs = []
    skipped = []
    if not raw:
        return pairs, skipped

    # cookie 名称的合法字符集（RFC 6265 token），据此判断一段是否为新 cookie 的开头
    name_pattern = re.compile(r'^[A-Za-z0-9!#$%&\'*+\-.^_`|~]+$')

    for chunk in re.split(r'[;\r\n]+', raw):
        segment = chunk.strip()
        if not segment:
            continue

        name, sep, value = segment.partition('=')
        is_new_cookie = bool(sep) and bool(name_pattern.match(name.strip()))

        if is_new_cookie:
            pairs.append([name.strip(), value.strip()])
        elif pairs:
            # 不像新 cookie，说明上一个 cookie 的值里含分号或换行，拼回去
            pairs[-1][1] = f"{pairs[-1][1]};{segment}"
        else:
            # 开头就是异常片段，无法归属，只报告长度不输出内容
            skipped.append(f"开头的异常片段（缺少合法 cookie 名），长度 {len(segment)}")

    result = []
    for name, value in pairs:
        if should_skip_cookie(name):
            skipped.append(f"{name}（与本机环境绑定或与登录态无关）")
            continue
        result.append((name, value))

    return result, skipped


def parse_chrome_major_version(version_output):
    """
    从 `chrome --version` 的输出中解析大版本号。
    输入形如 "Google Chrome 150.0.7871.128"，返回 150；无法解析时返回 None。
    """
    if not version_output:
        return None
    match = re.search(r'(\d+)\.\d+\.\d+', version_output)
    return int(match.group(1)) if match else None


def detect_chrome_major_version():
    """
    探测本机已安装 Chrome 的大版本号，用于让驱动版本与浏览器保持一致。
    允许通过 CHROME_MAJOR_VERSION 直接指定，便于在版本探测失败时人工兜底。
    探测失败返回 None，由调用方回退到自动匹配。
    """
    override = os.environ.get("CHROME_MAJOR_VERSION", "").strip()
    if override.isdigit():
        return int(override)

    for binary in ("google-chrome", "chromium-browser", "chromium", "chrome"):
        executable = shutil.which(binary)
        if not executable:
            continue
        try:
            output = subprocess.run(
                [executable, "--version"],
                capture_output=True,
                text=True,
                timeout=15,
            ).stdout
        except Exception as e:
            print(f"探测 {binary} 版本失败: {str(e)}")
            continue

        version = parse_chrome_major_version(output)
        if version:
            return version

    return None


ns_random = env_bool("NS_RANDOM")
cookie = os.environ.get("NS_COOKIE") or os.environ.get("COOKIE")
# 通过环境变量控制是否使用无头模式，默认为 True（无头模式）
headless = env_bool("HEADLESS", default=True)
# 除签到外的任务（评论、加鸡腿）总开关，默认关闭。
# 这些操作有被举报禁言的风险，需显式设置 NS_EXTRA_TASKS=true 才执行。
extra_tasks_enabled = env_bool("NS_EXTRA_TASKS")

randomInputStr = ["bd","绑定","帮顶"]

# Cloudflare 挑战页（"Just a moment..." 5 秒盾）的特征。
# 命中任一即说明当前页面不是论坛正文，此时任何元素定位都必然超时。
CF_CHALLENGE_MARKERS = ("just a moment", "challenges.cloudflare.com", "cf-browser-verification")

# 签到页地址。直接导航到此页，避免点击头部签到图标时被 #nsk-head 容器遮挡。
SIGN_PAGE_URL = 'https://www.nodeseek.com/signIn.html'

# 页面已签到的文案特征。命中任一说明今日已领取，属于正常结果而非失败。
SIGNED_MARKERS = ("今日已签到", "已经签到", "已签到", "明天再来", "请明天")


def is_cloudflare_challenge(driver):
    """判断当前页面是否停留在 Cloudflare 挑战页。"""
    try:
        title = (driver.title or "").lower()
        if any(marker in title for marker in CF_CHALLENGE_MARKERS):
            return True
        # 挑战页体积很小，只截取头部即可判断，避免拉取整页源码
        head = (driver.page_source or "")[:3000].lower()
        return any(marker in head for marker in CF_CHALLENGE_MARKERS)
    except Exception as e:
        print(f"检测 Cloudflare 挑战页失败: {str(e)}")
        return False


def wait_for_cloudflare(driver, timeout=60):
    """
    等待 Cloudflare 挑战自行通过。
    undetected-chromedriver 通常能自动过盾，但需要给它时间；
    这里轮询直到页面不再是挑战页，超时返回 False 由调用方决定如何处理。
    """
    if not is_cloudflare_challenge(driver):
        return True

    print(f"检测到 Cloudflare 挑战页，最多等待 {timeout} 秒...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(3)
        if not is_cloudflare_challenge(driver):
            print("Cloudflare 挑战已通过")
            return True

    print("Cloudflare 挑战在超时内未通过")
    return False


def extract_sign_reward(driver):
    """
    从签到页面文本中提取鸡腿收益描述，用于通知正文。
    页面文案可能随站点调整，提取失败时返回空字符串，不影响主流程。
    """
    try:
        page_text = BeautifulSoup(driver.page_source, 'html.parser').get_text(' ', strip=True)
        match = re.search(r'[^。；;\s]{0,20}?\d+\s*个?鸡腿[^。；;]{0,20}', page_text)
        return match.group(0).strip() if match else ""
    except Exception as e:
        print(f"提取签到收益失败: {str(e)}")
        return ""

def detect_already_signed(driver):
    """
    判断签到页是否已显示"今日已签到"之类的文案。
    用于区分"确实签过了"与"点击没生效"，避免后者被误报为成功。
    """
    try:
        text = BeautifulSoup(driver.page_source, 'html.parser').get_text(' ', strip=True)
        return any(marker in text for marker in SIGNED_MARKERS)
    except Exception as e:
        print(f"检测已签到状态失败: {str(e)}")
        return False


def detect_login_required(driver):
    """
    判断当前是否处于未登录状态，说明 cookie 已失效。

    不能只看正文里有没有"登录"二字——已登录的论坛页面顶部也有"登录/注册"入口，
    会误判。改用更可靠的信号：
    1. 页面 <title> 包含"登录"二字（真实登录页标题形如 NodeSeek-登录）；
    2. current_url 落到登录/注册路径（cookie 失效时常被重定向过去）。
    """
    try:
        title = (driver.title or "")
        if "登录" in title or "login" in title.lower():
            return True
        current_url = (driver.current_url or "").lower()
        login_paths = ("/login", "/signin", "/sign-in", "/register", "/signup")
        if any(path in current_url for path in login_paths):
            # signIn.html 本身就是签到页，排除掉，避免签到页 URL 自带 "signin" 被误判
            # 注意签到页正常情况下不要求登录，真正失效才会跳到登录页或标题变 NodeSeek-登录
            return False
        return False
    except Exception as e:
        print(f"检测登录状态失败: {str(e)}")
        return False


def click_sign_icon(driver):
    """
    执行签到：直接打开签到页并领取奖励。

    不再点击头部的签到图标——该图标会被 #nsk-head 容器遮挡导致
    element click intercepted，直接导航到签到页可绕开遮挡。

    返回: {"success": bool, "detail": str}，detail 为通知用的中文结果描述。
    只有确认领取成功或页面明确显示已签到才算成功；
    既没领到又没有已签到标志时一律视为失败，避免掩盖真实问题。
    """
    try:
        print(f"正在打开签到页: {SIGN_PAGE_URL}")
        driver.get(SIGN_PAGE_URL)

        # 签到页同样可能被 Cloudflare 拦下
        if not wait_for_cloudflare(driver):
            return {"success": False, "detail": "签到失败: 未能通过 Cloudflare 挑战"}

        time.sleep(2)
        print(f"当前页面URL: {driver.current_url}")

        if detect_login_required(driver):
            return {"success": False, "detail": "签到失败: cookie 已失效，需要重新登录"}

        # 先看是否已经签过，已签过时页面不会再有领取按钮
        if detect_already_signed(driver):
            print("页面显示今日已签到")
            return {"success": True, "detail": "今日已签到"}

        button_text = '试试手气' if ns_random else '鸡腿 x 5'
        print(f"查找领取按钮: {button_text}")
        try:
            click_button = WebDriverWait(driver, 15).until(
                EC.element_to_be_clickable((By.XPATH, f"//button[contains(text(), '{button_text}')]"))
            )
        except Exception as find_error:
            # 既没有已签到标志又找不到按钮，属于异常状态，必须报失败而非静默成功
            print(f"未找到领取按钮: {str(find_error)}")
            print(f"当前页面源码片段: {driver.page_source[:500]}...")
            return {"success": False, "detail": f"签到失败: 未找到领取按钮（{button_text}），且页面无已签到标志"}

        # 滚动到按钮再点击，避免被固定头部遮挡；原生点击失败时回退 JS 点击
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", click_button)
        time.sleep(0.5)
        try:
            click_button.click()
        except Exception as click_error:
            print(f"原生点击失败，改用 JavaScript 点击: {str(click_error)}")
            driver.execute_script("arguments[0].click();", click_button)

        print("已点击领取按钮，等待结果...")
        time.sleep(3)

        # 校验结果：拿到收益描述或出现已签到标志才算成功
        reward = extract_sign_reward(driver)
        if reward:
            print(f"签到成功: {reward}")
            return {"success": True, "detail": f"签到成功，{reward}"}

        if detect_already_signed(driver):
            print("点击后页面显示已签到")
            return {"success": True, "detail": "签到成功"}

        print(f"点击后未能确认结果，页面源码片段: {driver.page_source[:500]}...")
        return {"success": False, "detail": "签到失败: 已点击领取按钮但未能确认签到结果"}

    except Exception as e:
        print(f"签到过程中出错:")
        print(f"错误类型: {type(e).__name__}")
        print(f"错误信息: {str(e)}")
        print(f"当前页面URL: {driver.current_url}")
        print(f"当前页面源码片段: {driver.page_source[:500]}...")
        print("详细错误信息:")
        traceback.print_exc()
        return {"success": False, "detail": f"签到失败: {type(e).__name__} {str(e)}"}

def setup_driver_and_cookies():
    """
    初始化浏览器并设置cookie的通用方法
    返回: 设置好cookie的driver实例
    """
    try:
        cookie = os.environ.get("NS_COOKIE") or os.environ.get("COOKIE")
        headless = env_bool("HEADLESS", default=True)

        if not cookie:
            print("未找到cookie配置")
            return None
            
        print("开始初始化浏览器...")
        options = uc.ChromeOptions()
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        # 以下参数与是否无头无关，始终降低自动化特征。
        # 不覆盖 User-Agent：伪造的 UA 若与真实平台和 Chrome 版本不一致，
        # 反而会成为 Cloudflare 的识别特征，让 undetected-chromedriver 使用真实 UA。
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_argument('--window-size=1920,1080')

        if headless:
            # 无头模式指纹更容易被 Cloudflare 识别。
            # 在 GitHub Actions 中建议改用 xvfb-run 提供虚拟显示并令 HEADLESS=false，
            # 以有头浏览器运行，通过挑战的概率明显更高。
            print("启用无头模式（Cloudflare 拦截概率较高）...")
            options.add_argument('--headless=new')
            options.add_argument('--disable-gpu')
        else:
            print("使用有头模式（需要可用的显示环境，如 xvfb）...")

        print("正在启动Chrome...")
        # undetected-chromedriver 默认下载最新版驱动，而 runner 预装的 Chrome 往往落后一个大版本，
        # 二者不匹配会直接抛 SessionNotCreatedException。显式传入实际大版本号强制取匹配的驱动。
        version_main = detect_chrome_major_version()
        if version_main:
            print(f"检测到 Chrome 大版本: {version_main}，将使用匹配的驱动")
            driver = uc.Chrome(options=options, version_main=version_main)
        else:
            print("未能检测到 Chrome 版本，回退为自动匹配")
            driver = uc.Chrome(options=options)

        # 隐藏 webdriver 标记，有头/无头模式都需要
        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        driver.set_window_size(1920, 1080)

        print("Chrome启动成功")

        print("正在设置cookie...")
        driver.get('https://www.nodeseek.com')

        # 首次访问可能落在 Cloudflare 挑战页，需等其自动放行后再注入 cookie
        wait_for_cloudflare(driver)

        pairs, skipped = parse_cookie_string(cookie)
        for reason in skipped:
            print(f"跳过 cookie: {reason}")

        injected = 0
        for name, value in pairs:
            try:
                driver.add_cookie({
                    'name': name,
                    'value': value,
                    'domain': '.nodeseek.com',
                    'path': '/'
                })
                injected += 1
            except Exception as e:
                print(f"注入 cookie {name} 失败: {str(e)}")
                continue

        # 只打印名称不打印值，便于比对配置是否完整而不泄漏凭据
        print(f"共注入 {injected} 个 cookie: {[name for name, _ in pairs]}")
        if injected == 0:
            # 一个都没注入必然无法登录，提前失败比后续在签到步骤报错更容易定位
            print("没有任何有效 cookie 被注入，请检查 NS_COOKIE 格式（应形如 session=xxx）")
            driver.quit()
            return None

        if not any(name.lower() == 'session' for name, _ in pairs):
            # session 是登录态所在，缺失时后续必然停在未登录页面，提前点明原因
            print("警告: 未注入名为 session 的 cookie，登录态很可能不完整")

        print("刷新页面...")
        driver.refresh()
        time.sleep(5)  # 增加等待时间

        # 带上登录态后可能再次遇到挑战，这里等待通过后再交给后续任务
        if not wait_for_cloudflare(driver):
            print("Cloudflare 挑战未通过，后续操作很可能失败")

        return driver
        
    except Exception as e:
        print(f"设置浏览器和Cookie时出错: {str(e)}")
        print("详细错误信息:")
        print(traceback.format_exc())
        return None

def nodeseek_comment(driver):
    """
    在交易区随机帖子下评论并尝试加鸡腿
    返回: {"total": int, "commented": int, "chicken_leg": bool, "error": str}
    """
    stats = {"total": 0, "commented": 0, "chicken_leg": False, "error": ""}
    try:
        print("正在访问交易区...")
        target_url = 'https://www.nodeseek.com/categories/trade'
        driver.get(target_url)
        print("等待页面加载...")
        
        # 获取初始帖子列表
        posts = WebDriverWait(driver, 30).until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, '.post-list-item'))
        )
        print(f"成功获取到 {len(posts)} 个帖子")
        
        # 过滤掉置顶帖
        valid_posts = [post for post in posts if not post.find_elements(By.CSS_SELECTOR, '.pined')]
        selected_posts = random.sample(valid_posts, min(20, len(valid_posts)))
        
        # 存储已选择的帖子URL
        selected_urls = []
        for post in selected_posts:
            try:
                post_link = post.find_element(By.CSS_SELECTOR, '.post-title a')
                selected_urls.append(post_link.get_attribute('href'))
            except:
                continue
        
        is_chicken_leg = False
        stats["total"] = len(selected_urls)

        # 使用URL列表进行操作
        for i, post_url in enumerate(selected_urls):
            try:
                print(f"正在处理第 {i+1} 个帖子")
                driver.get(post_url)
                
                # 处理加鸡腿
                if is_chicken_leg is False:
                    is_chicken_leg = click_chicken_leg(driver)
                
                # 等待 CodeMirror 编辑器加载
                editor = WebDriverWait(driver, 30).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, '.CodeMirror'))
                )
                
                # 点击编辑器区域获取焦点
                editor.click()
                time.sleep(0.5)
                input_text = random.choice(randomInputStr)

                # 模拟输入
                actions = ActionChains(driver)
                # 随机输入 randomInputStr
                for char in input_text:
                    actions.send_keys(char)
                    actions.pause(random.uniform(0.1, 0.3))
                actions.perform()
                
                # 等待一下确保内容已经输入
                time.sleep(2)
                
                # 使用更精确的选择器定位提交按钮
                submit_button = WebDriverWait(driver, 30).until(
                 EC.element_to_be_clickable((By.XPATH, "//button[contains(@class, 'submit') and contains(@class, 'btn') and contains(text(), '发布评论')]"))
                )
                # 确保按钮可见并可点击
                driver.execute_script("arguments[0].scrollIntoView(true);", submit_button)
                time.sleep(0.5)
                submit_button.click()
                
                stats["commented"] += 1
                print(f"已在帖子 {post_url} 中完成评论")

                # 返回交易区
                # driver.get(target_url)
                # time.sleep(2)  # 等待页面加载
                time.sleep(random.uniform(2,5))
                
            except Exception as e:
                print(f"处理帖子时出错: {str(e)}")
                continue
                
        stats["chicken_leg"] = is_chicken_leg
        print("NodeSeek评论任务完成")

    except Exception as e:
        stats["error"] = f"{type(e).__name__} {str(e)}"
        print(f"NodeSeek评论出错: {str(e)}")
        print("详细错误信息:")
        print(traceback.format_exc())

    return stats


def build_notify_content(sign_result, comment_stats):
    """把签到与评论结果拼成通知正文（纯文本，各渠道通用）。"""
    lines = [
        f"执行时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"签到结果: {sign_result['detail']}",
    ]

    # 附加任务被开关关闭时只说明状态，不输出无意义的 0/0 统计
    if comment_stats is None:
        lines.append("附加任务: 已关闭（NS_EXTRA_TASKS 未开启）")
        return "\n".join(lines)

    if comment_stats["error"]:
        lines.append(f"评论任务: 异常终止（{comment_stats['error']}）")
    else:
        lines.append(
            f"评论任务: 成功 {comment_stats['commented']}/{comment_stats['total']} 个帖子"
        )
    lines.append(f"加鸡腿: {'成功' if comment_stats['chicken_leg'] else '未成功'}")
    return "\n".join(lines)

def click_chicken_leg(driver):
    try:
        print("尝试点击加鸡腿按钮...")
        chicken_btn = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.XPATH, '//div[@class="nsk-post"]//div[@title="加鸡腿"][1]'))
        )
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", chicken_btn)
        time.sleep(0.5)
        chicken_btn.click()
        print("加鸡腿按钮点击成功")
        
        # 等待确认对话框出现
        WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, '.msc-confirm'))
        )
        
        # 检查是否是7天前的帖子
        try:
            error_title = driver.find_element(By.XPATH, "//h3[contains(text(), '该评论创建于7天前')]")
            if error_title:
                print("该帖子超过7天，无法加鸡腿")
                ok_btn = driver.find_element(By.CSS_SELECTOR, '.msc-confirm .msc-ok')
                ok_btn.click()
                return False
        except:
            ok_btn = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, '.msc-confirm .msc-ok'))
            )
            ok_btn.click()
            print("确认加鸡腿成功")
            
        # 等待确认对话框消失
        WebDriverWait(driver, 5).until_not(
            EC.presence_of_element_located((By.CSS_SELECTOR, '.msc-overlay'))
        )
        time.sleep(1)  # 额外等待以确保对话框完全消失
        
        return True
        
    except Exception as e:
        print(f"加鸡腿操作失败: {str(e)}")
        return False

def run():
    """
    执行每日任务并推送通知。
    返回进程退出码：0 表示签到成功，1 表示浏览器初始化失败或签到失败。
    """
    print("开始执行NodeSeek每日任务...")
    driver = setup_driver_and_cookies()
    if not driver:
        print("浏览器初始化失败")
        # 初始化失败同样推送通知，避免任务静默中断
        notify.send("NodeSeek 每日任务失败", "浏览器初始化失败，请检查 NS_COOKIE 配置与运行环境")
        return 1

    # 评论与加鸡腿受 NS_EXTRA_TASKS 控制，关闭时只执行签到
    if extra_tasks_enabled:
        print("NS_EXTRA_TASKS 已开启，执行评论与加鸡腿任务")
        comment_stats = nodeseek_comment(driver)
    else:
        print("NS_EXTRA_TASKS 未开启，跳过评论与加鸡腿任务，仅执行签到")
        comment_stats = None

    sign_result = click_sign_icon(driver)
    print("脚本执行完成")

    title = "NodeSeek 每日任务" + ("" if sign_result["success"] else "（签到异常）")
    notify.send(title, build_notify_content(sign_result, comment_stats))
    return 0 if sign_result["success"] else 1


if __name__ == "__main__":
    exit(run())

