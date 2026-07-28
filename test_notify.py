# -- coding: utf-8 --
"""通知模块单元测试。

用桩替换 requests.request，覆盖以下场景：
- 未配置渠道时跳过
- Telegram / 企业微信机器人 / 企业微信应用 正常推送
- 接口返回业务错误时标记失败且不影响其他渠道
- 超长内容按字符数与字节数截断
"""
import unittest
from unittest import mock

import notify

# 各渠道所有相关环境变量，测试前统一清空避免真实配置干扰
ENV_KEYS = (
    "TG_BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "TG_USER_ID", "TG_CHAT_ID", "TELEGRAM_CHAT_ID",
    "TG_API_HOST", "TG_PROXY",
    "WECOM_WEBHOOK", "WECHAT_WEBHOOK", "QYWX_WEBHOOK",
    "WECOM_CORPID", "QYWX_CORPID", "WECOM_CORPSECRET", "QYWX_CORPSECRET",
    "WECOM_AGENTID", "QYWX_AGENTID", "WECOM_TOUSER", "QYWX_TOUSER",
)


class FakeResponse:
    """模拟 requests.Response，仅实现被 notify 使用的接口。"""

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class NotifyTestCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict("os.environ", {key: "" for key in ENV_KEYS}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        # 重试等待会拖慢测试，直接跳过 sleep
        sleep_patcher = mock.patch("notify.time.sleep")
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

    def test_未配置渠道时全部跳过(self):
        with mock.patch("notify.requests.request") as request:
            results = notify.send("标题", "正文")
        request.assert_not_called()
        self.assertEqual(
            results, {"Telegram": "skipped", "企业微信机器人": "skipped", "企业微信应用": "skipped"}
        )

    def test_telegram_推送成功并携带正确参数(self):
        with mock.patch.dict("os.environ", {"TG_BOT_TOKEN": "token123", "TG_USER_ID": "456"}):
            with mock.patch(
                "notify.requests.request", return_value=FakeResponse({"ok": True})
            ) as request:
                results = notify.send("标题", "正文")

        self.assertEqual(results["Telegram"], "sent")
        method, url = request.call_args.args
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://api.telegram.org/bottoken123/sendMessage")
        payload = request.call_args.kwargs["json"]
        self.assertEqual(payload["chat_id"], "456")
        self.assertEqual(payload["text"], "标题\n\n正文")

    def test_企业微信机器人推送成功(self):
        webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"
        with mock.patch.dict("os.environ", {"WECOM_WEBHOOK": webhook}):
            with mock.patch(
                "notify.requests.request", return_value=FakeResponse({"errcode": 0})
            ) as request:
                results = notify.send("标题", "正文")

        self.assertEqual(results["企业微信机器人"], "sent")
        self.assertEqual(request.call_args.args[1], webhook)
        self.assertEqual(request.call_args.kwargs["json"]["text"]["content"], "标题\n\n正文")

    def test_企业微信应用先取token再发消息(self):
        env = {"WECOM_CORPID": "corp", "WECOM_CORPSECRET": "secret", "WECOM_AGENTID": "1000002"}
        responses = [FakeResponse({"access_token": "tk"}), FakeResponse({"errcode": 0})]
        with mock.patch.dict("os.environ", env):
            with mock.patch("notify.requests.request", side_effect=responses) as request:
                results = notify.send("标题", "正文")

        self.assertEqual(results["企业微信应用"], "sent")
        self.assertEqual(request.call_count, 2)
        send_call = request.call_args_list[1]
        self.assertEqual(send_call.kwargs["params"]["access_token"], "tk")
        self.assertEqual(send_call.kwargs["json"]["agentid"], 1000002)
        self.assertEqual(send_call.kwargs["json"]["touser"], "@all")

    def test_单渠道失败不影响其他渠道(self):
        env = {
            "TG_BOT_TOKEN": "token",
            "TG_USER_ID": "1",
            "WECOM_WEBHOOK": "https://example.com/hook",
        }

        def fake_request(method, url, **kwargs):
            if "telegram" in url:
                return FakeResponse({"ok": False, "description": "chat not found"})
            return FakeResponse({"errcode": 0})

        with mock.patch.dict("os.environ", env):
            with mock.patch("notify.requests.request", side_effect=fake_request):
                results = notify.send("标题", "正文")

        self.assertIn("失败", results["Telegram"])
        self.assertEqual(results["企业微信机器人"], "sent")

    def test_网络异常重试到上限后标记失败(self):
        with mock.patch.dict("os.environ", {"TG_BOT_TOKEN": "t", "TG_USER_ID": "1"}):
            with mock.patch(
                "notify.requests.request", side_effect=RuntimeError("连接超时")
            ) as request:
                results = notify.send("标题", "正文")

        self.assertEqual(request.call_count, notify.MAX_RETRY)
        self.assertIn("连接超时", results["Telegram"])

    def test_超长内容按字符数截断(self):
        long_text = "长" * 5000
        with mock.patch.dict("os.environ", {"TG_BOT_TOKEN": "t", "TG_USER_ID": "1"}):
            with mock.patch(
                "notify.requests.request", return_value=FakeResponse({"ok": True})
            ) as request:
                notify.send("标题", long_text)

        text = request.call_args.kwargs["json"]["text"]
        self.assertEqual(len(text), notify.TELEGRAM_TEXT_LIMIT)
        self.assertTrue(text.endswith("..."))

    def test_超长内容按字节数截断(self):
        long_text = "长" * 5000
        with mock.patch.dict("os.environ", {"WECOM_WEBHOOK": "https://example.com/hook"}):
            with mock.patch(
                "notify.requests.request", return_value=FakeResponse({"errcode": 0})
            ) as request:
                notify.send("标题", long_text)

        content = request.call_args.kwargs["json"]["text"]["content"]
        self.assertLessEqual(len(content.encode("utf-8")), notify.WECOM_TEXT_BYTES_LIMIT)
        self.assertTrue(content.endswith("..."))


if __name__ == "__main__":
    unittest.main(verbosity=2)
