# -- coding: utf-8 --
"""通知推送模块。

按环境变量启用推送渠道，未配置的渠道自动跳过，任一渠道失败不影响其他渠道。
支持渠道：
- Telegram Bot（TG_BOT_TOKEN + TG_USER_ID）
- 企业微信群机器人（WECOM_WEBHOOK）
- 企业微信应用消息（WECOM_CORPID + WECOM_CORPSECRET + WECOM_AGENTID）
"""
import os
import time

import requests

# 单次请求超时秒数与失败重试次数（网络抖动时重试，指数退避）
REQUEST_TIMEOUT = 20
MAX_RETRY = 3

# Telegram 单条消息上限 4096 字符，企业微信 text 正文上限 2048 字节，均预留余量
TELEGRAM_TEXT_LIMIT = 4000
WECOM_TEXT_BYTES_LIMIT = 1900

WECOM_API = "https://qyapi.weixin.qq.com/cgi-bin"


def _env(*names, default=""):
    """按顺序取第一个非空环境变量，用于兼容多种命名习惯。"""
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return default


def _truncate_chars(text, limit):
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _truncate_bytes(text, limit):
    """按 UTF-8 字节数截断，避免企业微信因超长拒收。"""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[: limit - 3].decode("utf-8", errors="ignore") + "..."


def _request(method, url, **kwargs):
    """带重试的 HTTP 请求，返回解析后的 JSON（非 JSON 响应抛异常）。"""
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    last_error = None
    for attempt in range(1, MAX_RETRY + 1):
        try:
            response = requests.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json()
        except Exception as error:
            last_error = error
            if attempt < MAX_RETRY:
                time.sleep(2 * attempt)
    raise last_error


def send_telegram(title, content):
    """推送到 Telegram。TG_API_HOST 可指向自建反代，TG_PROXY 走 HTTP 代理。"""
    token = _env("TG_BOT_TOKEN", "TELEGRAM_BOT_TOKEN")
    chat_id = _env("TG_USER_ID", "TG_CHAT_ID", "TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return None

    api_host = _env("TG_API_HOST", default="https://api.telegram.org").rstrip("/")
    proxy = _env("TG_PROXY")
    proxies = {"http": proxy, "https": proxy} if proxy else None

    payload = {
        "chat_id": chat_id,
        "text": _truncate_chars(f"{title}\n\n{content}", TELEGRAM_TEXT_LIMIT),
        "disable_web_page_preview": True,
    }
    result = _request("POST", f"{api_host}/bot{token}/sendMessage", json=payload, proxies=proxies)
    if not result.get("ok"):
        raise RuntimeError(f"Telegram 返回失败: {result.get('description') or result}")
    return True


def send_wecom_webhook(title, content):
    """推送到企业微信群机器人。"""
    webhook = _env("WECOM_WEBHOOK", "WECHAT_WEBHOOK", "QYWX_WEBHOOK")
    if not webhook:
        return None

    payload = {
        "msgtype": "text",
        "text": {"content": _truncate_bytes(f"{title}\n\n{content}", WECOM_TEXT_BYTES_LIMIT)},
    }
    result = _request("POST", webhook, json=payload)
    if result.get("errcode") != 0:
        raise RuntimeError(f"企业微信机器人返回失败: {result}")
    return True


def send_wecom_app(title, content):
    """推送到企业微信自建应用，默认发给应用可见范围内全部成员。"""
    corp_id = _env("WECOM_CORPID", "QYWX_CORPID")
    corp_secret = _env("WECOM_CORPSECRET", "QYWX_CORPSECRET")
    agent_id = _env("WECOM_AGENTID", "QYWX_AGENTID")
    if not (corp_id and corp_secret and agent_id):
        return None

    token_result = _request(
        "GET", f"{WECOM_API}/gettoken", params={"corpid": corp_id, "corpsecret": corp_secret}
    )
    access_token = token_result.get("access_token")
    if not access_token:
        raise RuntimeError(f"企业微信获取 access_token 失败: {token_result}")

    payload = {
        "touser": _env("WECOM_TOUSER", "QYWX_TOUSER", default="@all"),
        "msgtype": "text",
        "agentid": int(agent_id),
        "text": {"content": _truncate_bytes(f"{title}\n\n{content}", WECOM_TEXT_BYTES_LIMIT)},
        "duplicate_check_interval": 600,
    }
    result = _request(
        "POST", f"{WECOM_API}/message/send", params={"access_token": access_token}, json=payload
    )
    if result.get("errcode") != 0:
        raise RuntimeError(f"企业微信应用消息返回失败: {result}")
    return True


CHANNELS = (
    ("Telegram", send_telegram),
    ("企业微信机器人", send_wecom_webhook),
    ("企业微信应用", send_wecom_app),
)


def send(title, content):
    """向所有已配置渠道推送，返回 {渠道名: "sent"/"skipped"/错误信息}。"""
    results = {}
    for name, sender in CHANNELS:
        try:
            sent = sender(title, content)
        except Exception as error:
            results[name] = f"失败: {error}"
            print(f"[通知] {name} 推送失败: {error}")
            continue

        if sent is None:
            results[name] = "skipped"
        else:
            results[name] = "sent"
            print(f"[通知] {name} 推送成功")

    if all(state == "skipped" for state in results.values()):
        print("[通知] 未配置任何推送渠道，跳过通知")
    return results
