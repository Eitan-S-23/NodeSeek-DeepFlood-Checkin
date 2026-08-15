# -- coding: utf-8 --
"""linuxsb_daily 签到逻辑测试。

linuxsb_daily 只依赖 requests 与 notify，这里用 mock 替换网络请求，
使签到判断与多账号流程可以脱离真实站点独立验证。
"""
import unittest
from unittest import mock

import linuxsb_daily as daily


# 模拟签到页 HTML：未签到 + 含 CSRF
PAGE_UNCHECKED = (
    '<html><body>'
    '<input type="hidden" name="_csrf" value="abc123csrf">'
    '<button>每日签到</button>'
    '</body></html>'
)
# 模拟签到页 HTML：已签到
PAGE_CHECKED = (
    '<html><body>'
    '<input type="hidden" name="_csrf" value="abc123csrf">'
    '<span>今日已签到</span>'
    '</body></html>'
)


class FakeResponse:
    """模拟 requests.Response"""

    def __init__(self, status_code=200, text="", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json_data = json_data

    def json(self):
        return self._json_data


def fake_get(page_html, status_code=200):
    """构造 GET 请求的 mock"""

    def handler(url, headers=None, cookies=None, timeout=None):
        return FakeResponse(status_code=status_code, text=page_html)

    return mock.patch.object(daily.requests, "get", side_effect=handler)


def fake_post(json_data, status_code=200):
    """构造 POST 请求的 mock"""

    def handler(url, headers=None, cookies=None, data=None, timeout=None):
        return FakeResponse(status_code=status_code, json_data=json_data)

    return mock.patch.object(daily.requests, "post", side_effect=handler)


class FetchCheckinStateTestCase(unittest.TestCase):
    """签到页解析测试"""

    def test_提取csrf且未签到(self):
        with fake_get(PAGE_UNCHECKED):
            csrf, checked_in = daily.fetch_checkin_state("a=1; b=2")
        self.assertEqual(csrf, "abc123csrf")
        self.assertFalse(checked_in)

    def test_识别已签到状态(self):
        with fake_get(PAGE_CHECKED):
            csrf, checked_in = daily.fetch_checkin_state("a=1")
        self.assertEqual(csrf, "abc123csrf")
        self.assertTrue(checked_in)

    def test_cookie失效时无csrf(self):
        with fake_get("<html>请登录</html>"):
            csrf, checked_in = daily.fetch_checkin_state("invalid=1")
        self.assertIsNone(csrf)
        self.assertFalse(checked_in)

    def test_页面错误抛异常(self):
        with fake_get("error", status_code=500):
            with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
                daily.fetch_checkin_state("a=1")


class SignInAccountTestCase(unittest.TestCase):
    """单账号签到流程测试"""

    def test_未签到进入post并成功(self):
        with fake_get(PAGE_UNCHECKED), fake_post({"ok": 1, "message": "签到成功"}):
            success, summary, username = daily.sign_in_account("a=1")
        self.assertTrue(success)
        self.assertIn("签到结果: 签到成功（签到成功）", summary)
        self.assertIsNone(username)  # 测试页面无用户名链接

    def test_已签到跳过post(self):
        with fake_get(PAGE_CHECKED) as get_mock, \
             mock.patch.object(daily.requests, "post") as post_mock:
            success, summary, _ = daily.sign_in_account("a=1")
        self.assertTrue(success)
        self.assertIn("今日已签到", summary)
        get_mock.assert_called()
        post_mock.assert_not_called()

    def test_cookie失效给出明确提示(self):
        with fake_get("<html>log in</html>"):
            success, summary, _ = daily.sign_in_account("bad=1")
        self.assertFalse(success)
        self.assertIn("Cookie 已失效", summary)

    def test_页面无csrf_从cookie的bbs_csrf兜底并成功(self):
        """页面结构变化不渲染 _csrf 时，用 cookie 中的 bbs_csrf 值完成签到"""
        page = '<html><body><button>每日签到</button></body></html>'
        with fake_get(page), \
             fake_post({"ok": 1, "message": "签到成功"}) as post_mock:
            success, summary, _ = daily.sign_in_account("bbs_auth=abc; bbs_csrf=cookiecsrf123")
        self.assertTrue(success)
        self.assertIn("签到成功", summary)
        # POST 携带的 _csrf 来自 cookie 中的 bbs_csrf
        sent_data = post_mock.call_args.kwargs["data"]
        self.assertEqual(sent_data["_csrf"], "cookiecsrf123")

    def test_重复签到时幂等视为成功(self):
        """服务端以 ok:0 + 已打卡/重复签到 返回时，视为当日已签到而非失败"""
        with fake_get(PAGE_UNCHECKED), \
             fake_post({"ok": 0, "message": "今日已打卡，请明天再来"}):
            success, summary, _ = daily.sign_in_account("a=1")
        self.assertTrue(success)
        self.assertIn("无需重复签到", summary)

    def test_post响应额外字段进入摘要(self):
        """ok:1 响应中的积分等字段一并展示（不同站点版本字段名不同）"""
        with fake_get(PAGE_UNCHECKED), \
             fake_post({"ok": 1, "message": "", "bonus": 10, "balance": 888}):
            success, summary, _ = daily.sign_in_account("a=1")
        self.assertTrue(success)
        self.assertIn("bonus: 10", summary)
        self.assertIn("balance: 888", summary)


class ExtractCheckinMetaTestCase(unittest.TestCase):
    """签到页概览信息提取测试"""

    def test_提取积分与连续签到(self):
        html = ('<div>当前积分：1,234</div><div>连续签到 5 天</div>'
                '<span>今日已签到</span>')
        found = dict(daily.extract_checkin_meta(html))
        self.assertEqual(found.get("当前积分"), "1,234")
        self.assertEqual(found.get("连续签到"), "5 天")

    def test_词与冒号间有空格的积分也可提取(self):
        html = '<div>积分 ： 88</div>'
        found = dict(daily.extract_checkin_meta(html))
        self.assertEqual(found.get("积分"), "88")

    def test_无积分信息时返回空(self):
        self.assertEqual(daily.extract_checkin_meta("<html>请先登录</html>"), [])

    def test_噪音词不误匹配积分(self):
        """「积分规则」「获得积分 +10」等不应被当作当前积分"""
        html = ('<div>积分规则：每日签到可获得积分</div>'
                '<div>获得积分 +10</div><div>当前积分：888</div>')
        found = dict(daily.extract_checkin_meta(html))
        self.assertEqual(found.get("当前积分"), "888")
        self.assertNotIn("积分", found)  # 命中了「当前积分」就不应再有重复的「积分」行

    def test_当前积分优先于积分字段(self):
        html = '<div>积分：123</div><div>当前积分：456</div>'
        found = dict(daily.extract_checkin_meta(html))
        self.assertEqual(found.get("当前积分"), "456")
        self.assertNotIn("积分", found)


class ExtractUsernameTestCase(unittest.TestCase):
    """用户名解析测试"""

    def test_从用户链接提取用户名(self):
        html = ('<a href="/user/42">小明同学</a>'
                '<span>每日签到</span>')
        self.assertEqual(daily.extract_username(html), "小明同学")

    def test_无用户链接时返回None(self):
        self.assertIsNone(daily.extract_username("<html>请先登录</html>"))


class ExtractUsernameRunTestCase(unittest.TestCase):
    """通知中账号标识测试：使用用户名，绝不暴露 cookie 内容"""

    def setUp(self):
        # 与 RunTestCase 一致：屏蔽 run() 签到的 SITE_GAP 随机延迟
        self.sleep_mock = mock.patch.object(daily.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        import os

        os.environ.pop("LINUXSB_COOKIE", None)

    def test_通知使用用户名而非cookie键名(self):
        import os

        os.environ["LINUXSB_COOKIE"] = "bbs_auth=abc; bbs_csrf=csrf123"
        page = ('<a href="/user/42">小明同学</a>'
                '<div>当前积分：888</div>'
                '<input type="hidden" name="_csrf" value="abc">')
        with fake_get(page), fake_post({"ok": 1, "message": "好"}), \
             mock.patch.object(daily.notify, "send") as send_mock:
            code = daily.run()
        self.assertEqual(code, 0)
        content = send_mock.call_args.args[1]
        self.assertIn("小明同学", content)
        # cookie 键名 bbs_auth / bbs_csrf 不进入通知
        self.assertNotIn("bbs_auth", content)
        self.assertNotIn("bbs_csrf", content)

    def test_服务端拒绝时返回失败不抛异常(self):
        with fake_get(PAGE_UNCHECKED), fake_post({"ok": 0, "message": "请求已过期"}):
            success, summary, _ = daily.sign_in_account("a=1")
        self.assertFalse(success)
        self.assertIn("请求已过期", summary)
        self.assertIn("更新 LINUXSB_COOKIE", summary)


class RunTestCase(unittest.TestCase):
    """主流程测试"""

    def setUp(self):
        # run() 签到前有 SITE_GAP 随机延迟（默认 60-180 秒），所有用例统一屏蔽，
        # 避免单测真实 sleep 拖慢执行；专门验证延迟的用例再单独断言。
        self.sleep_mock = mock.patch.object(daily.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        import os

        os.environ.pop("LINUXSB_COOKIE", None)

    def test_未配置cookie时静默跳过且不通知(self):
        with mock.patch.object(daily.notify, "send") as send_mock, \
             mock.patch.object(daily.time, "sleep") as sleep_mock:
            code = daily.run()
        self.assertEqual(code, 0)
        send_mock.assert_not_called()
        sleep_mock.assert_not_called()

    def test_多账号部分失败时退出码为1且单个失败不中断(self):
        import os

        os.environ["LINUXSB_COOKIE"] = "a=1&b=2"

        def get_handler(url, headers=None, cookies=None, timeout=None):
            # 账号1 Cookie 失效，账号2 正常
            if cookies and cookies.get("a") == "1":
                return FakeResponse(text="<html>请登录</html>")
            return FakeResponse(text=PAGE_UNCHECKED)

        with mock.patch.object(daily.requests, "get", side_effect=get_handler), \
             fake_post({"ok": 1, "message": "签到成功"}) as post_mock, \
             mock.patch.object(daily.notify, "send") as send_mock:
            code = daily.run()

        self.assertEqual(code, 1)
        # 只有账号2发起了签到 POST
        self.assertEqual(post_mock.call_count, 1)
        # 通知标题带「签到异常」，正文包含失败账号
        title, content = send_mock.call_args.args
        self.assertIn("签到异常", title)
        self.assertIn("账号 1", content)

    def test_所有账号成功时退出码为0(self):
        import os

        os.environ["LINUXSB_COOKIE"] = "a=1&b=2"
        with fake_get(PAGE_UNCHECKED), fake_post({"ok": 1, "message": "好"}), \
             mock.patch.object(daily.notify, "send") as send_mock:
            code = daily.run()
        self.assertEqual(code, 0)
        self.assertEqual(send_mock.call_args.args[0], "LinuxSB 每日任务")

    def test_通知内容对齐nodeseek分段格式(self):
        """通知含顶部执行时间、【linux.sb】分段与各账号概览"""
        import os

        os.environ["LINUXSB_COOKIE"] = "a=1"
        page = ('<div>当前积分：888</div><div>连续签到 3 天</div>'
                '<input type="hidden" name="_csrf" value="abc">')
        with fake_get(page), fake_post({"ok": 1, "message": ""}), \
             mock.patch.object(daily.notify, "send") as send_mock:
            code = daily.run()
        self.assertEqual(code, 0)
        content = send_mock.call_args.args[1]
        self.assertIn("执行时间: ", content)
        self.assertIn("【linux.sb】", content)
        self.assertIn("账号 1", content)
        self.assertIn("签到结果: 签到成功", content)
        self.assertIn("当前积分: 888", content)
        self.assertIn("连续签到: 3 天", content)

    def test_签到前有随机延迟(self):
        import os

        os.environ["LINUXSB_COOKIE"] = "a=1"
        with fake_get(PAGE_UNCHECKED), fake_post({"ok": 1, "message": "好"}), \
             mock.patch.object(daily.notify, "send"):
            daily.run()
        self.sleep_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)