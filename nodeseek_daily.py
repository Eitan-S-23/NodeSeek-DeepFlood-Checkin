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


ns_random = env_bool("NS_RANDOM")
cookie = os.environ.get("NS_COOKIE") or os.environ.get("COOKIE")
# 通过环境变量控制是否使用无头模式，默认为 True（无头模式）
headless = env_bool("HEADLESS", default=True)
# 除签到外的任务（评论、加鸡腿）总开关，默认关闭。
# 这些操作有被举报禁言的风险，需显式设置 NS_EXTRA_TASKS=true 才执行。
extra_tasks_enabled = env_bool("NS_EXTRA_TASKS")

randomInputStr = ["bd","绑定","帮顶"]

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

def click_sign_icon(driver):
    """
    尝试点击签到图标和试试手气按钮的通用方法
    返回: {"success": bool, "detail": str}，detail 为通知用的中文结果描述
    """
    try:
        print("开始查找签到图标...")
        # 使用更精确的选择器定位签到图标
        sign_icon = WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.XPATH, "//span[@title='签到']"))
        )
        print("找到签到图标，准备点击...")
        
        # 确保元素可见和可点击
        driver.execute_script("arguments[0].scrollIntoView(true);", sign_icon)
        time.sleep(0.5)
        
        # 打印元素信息
        print(f"签到图标元素: {sign_icon.get_attribute('outerHTML')}")
        
        # 尝试点击
        try:
            
            
            sign_icon.click()
            print("签到图标点击成功")
        except Exception as click_error:
            print(f"点击失败，尝试使用 JavaScript 点击: {str(click_error)}")
            driver.execute_script("arguments[0].click();", sign_icon)
        
        print("等待页面跳转...")
        time.sleep(5)
        
        # 打印当前URL
        print(f"当前页面URL: {driver.current_url}")
        
        # 点击"试试手气"按钮
        try:
            click_button:None
            
            if ns_random:
                click_button = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), '试试手气')]"))
            )
            else:
                click_button = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), '鸡腿 x 5')]"))
            )
            
            click_button.click()
            print("完成试试手气点击")
            time.sleep(2)
            reward = extract_sign_reward(driver)
            detail = f"签到成功，{reward}" if reward else "签到成功"
        except Exception as lucky_error:
            print(f"试试手气按钮点击失败或者签到过了: {str(lucky_error)}")
            detail = "今日已签到（未找到领取按钮）"

        return {"success": True, "detail": detail}

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
        headless = os.environ.get("HEADLESS", "true").lower() == "true"
        
        if not cookie:
            print("未找到cookie配置")
            return None
            
        print("开始初始化浏览器...")
        options = uc.ChromeOptions()
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        
        if headless:
            print("启用无头模式...")
            options.add_argument('--headless')
            # 添加以下参数来绕过 Cloudflare 检测
            options.add_argument('--disable-blink-features=AutomationControlled')
            options.add_argument('--disable-gpu')
            options.add_argument('--window-size=1920,1080')
            # 设置 User-Agent
            options.add_argument('--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
        
        print("正在启动Chrome...")
        driver = uc.Chrome(options=options)
        
        if headless:
            # 执行 JavaScript 来修改 webdriver 标记
            driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            driver.set_window_size(1920, 1080)
        
        print("Chrome启动成功")
        
        print("正在设置cookie...")
        driver.get('https://www.nodeseek.com')
        
        # 等待页面加载完成
        time.sleep(5)
        
        for cookie_item in cookie.split(';'):
            try:
                name, value = cookie_item.strip().split('=', 1)
                driver.add_cookie({
                    'name': name, 
                    'value': value, 
                    'domain': '.nodeseek.com',
                    'path': '/'
                })
            except Exception as e:
                print(f"设置cookie出错: {str(e)}")
                continue
        
        print("刷新页面...")
        driver.refresh()
        time.sleep(5)  # 增加等待时间
        
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

