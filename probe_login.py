# -- coding: utf-8 --
"""一次性诊断脚本：探测 NodeSeek 登录页结构，判断账号密码登录可行性。

只读页面、不提交表单、不碰凭据、不发通知。跑完据结果即可删除。
探测项：表单字段、验证码类型、是否 2FA、登录方式（用户名/手机/邮箱）。
"""
import time

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By


def probe():
    options = uc.ChromeOptions()
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-blink-features=AutomationControlled')
    options.add_argument('--window-size=1920,1080')
    # 有头模式配合 xvfb，过 Cloudflare 概率更高
    driver = uc.Chrome(options=options)
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    driver.set_window_size(1920, 1080)

    try:
        print("=== 打开 NodeSeek 登录页 ===", flush=True)
        driver.get('https://www.nodeseek.com/signIn.html')
        time.sleep(8)  # 给 Cloudflare 5 秒盾留时间
        print(f"当前 URL: {driver.current_url}", flush=True)
        print(f"页面 title: {driver.title}", flush=True)

        # 1. 表单输入框
        print("\n=== 输入框 ===", flush=True)
        inputs = driver.find_elements(By.CSS_SELECTOR, 'input')
        for inp in inputs:
            name = inp.get_attribute('name') or ''
            typ = inp.get_attribute('type') or ''
            ph = inp.get_attribute('placeholder') or ''
            print(f"  input name={name!r} type={typ!r} placeholder={ph!r}", flush=True)

        # 2. 验证码特征
        print("\n=== 验证码探测 ===", flush=True)
        page_html = (driver.page_source or '').lower()
        captcha_markers = {
            'Cloudflare Turnstile': ('challenges.cloudflare.com/turnstile', 'cf-turnstile', 'turnstile'),
            'hCaptcha': ('hcaptcha.com', 'h-captcha'),
            'reCAPTCHA': ('recaptcha', 'g-recaptcha'),
            '极验 geetest': ('geetest', 'gt_'),
        }
        found_captcha = False
        for label, markers in captcha_markers.items():
            if any(m in page_html for m in markers):
                print(f"  命中 {label}", flush=True)
                found_captcha = True
        if not found_captcha:
            print("  未发现已知验证码组件（可能登录无验证码，或异步加载）", flush=True)

        # 3. 2FA / 验证码字段
        print("\n=== 2FA / 动态验证字段 ===", flush=True)
        two_factor_hints = ('两步', '二次验证', '2fa', 'otp', 'totp', '验证码', 'code', 'authenticator')
        found_2fa = False
        for hint in two_factor_hints:
            if hint in page_html:
                print(f"  命中关键词: {hint!r}", flush=True)
                found_2fa = True
        if not found_2fa:
            print("  未发现 2FA 相关字段", flush=True)

        # 4. 登录方式线索
        print("\n=== 登录方式线索 ===", flush=True)
        for marker in ('手机', '邮箱', 'email', 'phone', 'username', '用户名', '账号'):
            if marker in page_html:
                print(f"  含关键词: {marker!r}", flush=True)

        # 5. 提交按钮
        print("\n=== 按钮 ===", flush=True)
        buttons = driver.find_elements(By.CSS_SELECTOR, 'button')
        for btn in buttons:
            print(f"  button: {btn.text!r}", flush=True)

    finally:
        driver.quit()


if __name__ == "__main__":
    probe()
