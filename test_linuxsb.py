# -- coding: utf-8 --
"""linuxsb_daily 签到逻辑测试。

linuxsb_daily 只依赖 requests 与 notify，这里用 mock 替换网络请求，
使签到判断与多账号流程可以脱离真实站点独立验证。
"""
import os
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

    def __init__(self, status_code=200, text="", json_data=None, url=None):
        self.status_code = status_code
        self.text = text
        self._json_data = json_data
        self.url = url

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
            csrf, checked_in, _ = daily.fetch_checkin_state("a=1; b=2")
        self.assertEqual(csrf, "abc123csrf")
        self.assertFalse(checked_in)

    def test_识别已签到状态(self):
        with fake_get(PAGE_CHECKED):
            csrf, checked_in, _ = daily.fetch_checkin_state("a=1")
        self.assertEqual(csrf, "abc123csrf")
        self.assertTrue(checked_in)

    def test_cookie失效时无csrf(self):
        with fake_get("<html>请登录</html>"):
            csrf, checked_in, _ = daily.fetch_checkin_state("invalid=1")
        self.assertIsNone(csrf)
        self.assertFalse(checked_in)

    def test_页面错误抛异常(self):
        with fake_get("error", status_code=500):
            with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
                daily.fetch_checkin_state("a=1")

    def test_登录页含csrf字段仍判定cookie失效(self):
        """登录页的登录表单同样带 name='_csrf'，不能当作有效签到凭据"""
        page = ('<form><input type="hidden" name="_csrf" value="logincsrf">'
                '<input name="username"><input name="password" type="password">'
                '</form><span>欢迎登录</span>')
        with fake_get(page):
            csrf, checked_in, _ = daily.fetch_checkin_state("bad=1")
        self.assertIsNone(csrf)
        self.assertFalse(checked_in)

    def test_重定向到登录页url时判定cookie失效(self):
        """未登录访问 /daily_checkin 会被 302 到 /login（requests 跟随后 url 为 /login）"""
        page = ('<input type="hidden" name="_csrf" value="logincsrf">'
                '<input name="password" type="password">')
        with mock.patch.object(
            daily.requests, "get",
            side_effect=lambda *a, **k: FakeResponse(
                status_code=200, text=page, url="https://linux.sb/login"
            ),
        ):
            csrf, _, is_login = daily.fetch_checkin_state("a=1")
        self.assertIsNone(csrf)
        self.assertTrue(is_login)


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

    def test_从个人信息卡提取用户名(self):
        """同源论坛程序的 user-name 卡片结构（登录态各页面通用）"""
        html = ('<div class="user-card">'
                '<a class="user-name" href="/user/42">烧饼爱好者</a>'
                '</div><div>当前积分：888</div>')
        self.assertEqual(daily.extract_username(html), "烧饼爱好者")

    def test_无用户信息时返回None(self):
        self.assertIsNone(daily.extract_username("<html>请先登录</html>"))

    def test_卡片结构优先于链接(self):
        html = ('<a class="user-name" href="/user/42">卡片用户名</a>'
                '<a href="/user/41">别人</a>')
        self.assertEqual(daily.extract_username(html), "卡片用户名")


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


class AccountLoginTestCase(unittest.TestCase):
    """账号密码兜底登录相关测试"""

    def tearDown(self):
        import os

        for name in ("LINUXSB_ACCOUNT", "LINUXSB_COOKIE"):
            os.environ.pop(name, None)

    def test_解析合法凭据(self):
        os.environ["LINUXSB_ACCOUNT"] = '{"username": "xiao ming", "password": "p@ss:word"}'
        self.assertEqual(daily.load_account_creds(),
                         {"username": "xiao ming", "password": "p@ss:word"})

    def test_未配置或非法JSON返回None(self):
        os.environ.pop("LINUXSB_ACCOUNT", None)
        self.assertIsNone(daily.load_account_creds())
        os.environ["LINUXSB_ACCOUNT"] = "not-json"
        self.assertIsNone(daily.load_account_creds())
        os.environ["LINUXSB_ACCOUNT"] = '{"username": ""}'
        self.assertIsNone(daily.load_account_creds())

    def test_算术题四则运算(self):
        cases = {
            "9 × 4 = ?": "36",
            "7 + 3 = ?": "10",
            "12 - 5 = ?": "7",
            "8 ÷ 2 = ?": "4",
            "3 * 4 = ?": "12",
        }
        for question, expected in cases.items():
            self.assertEqual(daily.solve_captcha_question(question), expected)

    def test_算术题无法解析抛异常(self):
        with self.assertRaises(ValueError):
            daily.solve_captcha_question("请输入验证码")


class NativeCaptchaPowTestCase(unittest.TestCase):
    """登录页 PoW 求解测试（与站点 plugins.js nativeCaptchaSolve 同算法）"""

    def test_pow前导零匹配(self):
        # prefix=0638ea1959f38c05 zeros=3，对照 .cache_login.html 真实题面
        nonce = daily.solve_native_captcha_pow("0638ea1959f38c05", 3)
        import hashlib
        digest = hashlib.sha256(("0638ea1959f38c05:" + nonce).encode()).hexdigest()
        self.assertTrue(digest.startswith("000"), f"nonce={nonce} digest={digest}")

    def test_zeros约束前导零位数(self):
        nonce = daily.solve_native_captcha_pow("abc", 2)
        import hashlib
        digest = hashlib.sha256(("abc:" + nonce).encode()).hexdigest()
        self.assertTrue(digest.startswith("00"))
        # zeros=2 不强制第三位为零
        self.assertNotEqual(digest[2], "0")

    def test_nonce为十六进制字符串(self):
        nonce = daily.solve_native_captcha_pow("p", 2)
        self.assertIsInstance(nonce, str)
        int(nonce, 16)  # 合法十六进制


class AccountsLoginTestCase(unittest.TestCase):
    """纯 requests 账号密码登录链路测试（mock 网络与字段提取）"""

    LOGIN_PAGE = (
        '<form method="post"><input type="hidden" name="_csrf" value="csrfABC">'
        '<input name="username"><input name="password" type="password">'
        '<div class="user-review-native-captcha" data-native-captcha '
        'data-pow-prefix="0638ea1959f38c05" data-pow-zeroes="3">'
        '<div class="native-captcha-question">11 - 4 = ?</div>'
        '<input name="native_captcha_answer">'
        '<input type="hidden" name="native_captcha_token" value="tok.eyJ2IjoxfQ">'
        '<input type="hidden" name="native_captcha_pow" value="">'
        '<input type="text" name="native_captcha_company">'
        '</div><button>登录</button></form>'
    )

    def _mock_session(self, get_html=None, post_html="", post_url="https://linux.sb/",
                       status_code=200, cookies=None):
        """构造 requests.Session 的 mock：GET 返回登录页 HTML，POST 返回登录结果。

        get_html 默认为本类的 LOGIN_PAGE（含完整反扒字段），便于登录链路提取。
        post_html / post_url / cookies 模拟登录提交后的响应与最终会话。
        """
        cookies = cookies or {}
        # 默认登录页用类常量；显式传入（如缺字段用例）则覆盖。捕获到局部变量，
        # 避免 _Session.get 内 self指错对象（嵌套类 self 指向 _Session 实例）
        login_html = get_html if get_html is not None else self.LOGIN_PAGE

        class _Resp:
            def __init__(self, text, url):
                self.status_code = status_code
                self.text = text
                self.url = url
                self.cookies = cookies

        class _Cookies(dict):
            """模拟 requests.Session.cookies（dict-like，带 items()）"""

            def items(self):
                return dict.items(self)

        class _Session:
            def __init__(self):
                self.headers = {}
                self.cookies = _Cookies(cookies)
                self.posted = None

            def get(self, *a, **k):
                return _Resp(login_html, "https://linux.sb/login")

            def post(self, url, headers=None, data=None, timeout=None, allow_redirects=True):
                self.posted = (url, data)
                return _Resp(post_html, post_url)

        sess = _Session()
        patcher = mock.patch.object(daily.requests, "Session", return_value=sess)
        return sess, patcher

    def test_登录成功返回会话cookie(self):
        sess, patcher = self._mock_session(
            post_html="<html>首页</html>", post_url="https://linux.sb/",
            cookies={"bbs_auth": "authed", "bbs_csrf": "csrf123"},
        )
        with patcher:
            cookie = daily.accounts_login({"username": "u", "password": "p"})
        self.assertIn("bbs_auth=authed", cookie)
        self.assertIn("bbs_csrf=csrf123", cookie)
        # POST 提交了完整表单，含本地解出的算术题答案与 PoW nonce、蜜罐留空
        url, data = sess.posted
        self.assertEqual(data["username"], "u")
        self.assertEqual(data["password"], "p")
        self.assertEqual(data["native_captcha_answer"], "7")  # 11-4
        self.assertEqual(data["native_captcha_company"], "")
        self.assertEqual(data["native_captcha_token"], "tok.eyJ2IjoxfQ")
        # PoW nonce 解出的摘要前 3 位为零
        import hashlib
        digest = hashlib.sha256(("0638ea1959f38c05:" + data["native_captcha_pow"]).encode()).hexdigest()
        self.assertTrue(digest.startswith("000"))

    def test_留在登录页判定失败(self):
        sess, patcher = self._mock_session(
            post_html="<html>登录失败</html>", post_url="https://linux.sb/login", cookies={},
        )
        with patcher:
            with self.assertRaisesRegex(RuntimeError, "停留在登录页"):
                daily.accounts_login({"username": "u", "password": "p"})

    def test_登录页缺字段抛异常(self):
        sess, patcher = self._mock_session(get_html="<html>无登录表单</html>")
        with patcher:
            with self.assertRaisesRegex(RuntimeError, "结构变化"):
                daily.accounts_login({"username": "u", "password": "p"})

    def test_登录后无cookie抛异常(self):
        sess, patcher = self._mock_session(
            post_html="<html>首页</html>", post_url="https://linux.sb/", cookies={},
        )
        with patcher:
            with self.assertRaisesRegex(RuntimeError, "未带会话 cookie"):
                daily.accounts_login({"username": "u", "password": "p"})


class RunLoginFallbackTestCase(unittest.TestCase):
    """run() 内 cookie 失效降级登录流程测试（账号密码登录后就地签到）"""

    def setUp(self):
        # 屏蔽 run() 签到的 SITE_GAP 随机延迟
        self.sleep_mock = mock.patch.object(daily.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        import os

        for name in ("LINUXSB_COOKIE", "LINUXSB_ACCOUNT"):
            os.environ.pop(name, None)

    def test_cookie失效时账号密码登录签到(self):
        os.environ["LINUXSB_COOKIE"] = "a=1"
        os.environ["LINUXSB_ACCOUNT"] = '{"username": "u", "password": "p"}'
        # 账号密码登录走 accounts_login（纯 requests），登录成功返回会话 cookie 后
        # 进入 sign_in_account 完成签到。两条路径都 mock：accounts_login 返回伪造
        # 会话 cookie，requests.get/post 由 fake_get/fake_post 接管签到页与签到 POST。
        # run() 的 cookie 探测先 GET 签到页（此处按 cookie 名 a=1 返回登录页特征
        # 「请登录」使 cookie_valid=False），accounts_login 返回伪造会话 cookie 后
        # sign_in_account 的概览 GET 再返回未签到签到页，两条 GET 路径分开。
        fake_session_cookie = "bbs_auth=authed; bbs_csrf=sessioncsrf"

        def get_handler(url, headers=None, cookies=None, timeout=None):
            # cookie 探测（带 a=1）判失效；登录后的概览 GET（带 bbs_auth）返回签到页
            if cookies and cookies.get("a") == "1":
                return FakeResponse(text="<html>请登录</html>")
            return FakeResponse(text=PAGE_UNCHECKED)

        with mock.patch.object(daily.requests, "get", side_effect=get_handler), \
             fake_post({"ok": 1, "message": "签到成功"}), \
             mock.patch.object(daily, "accounts_login",
                               return_value=fake_session_cookie) as login_mock, \
             mock.patch.object(daily.notify, "send") as send_mock:
            code = daily.run()

        self.assertEqual(code, 0)
        login_mock.assert_called_once()
        self.assertEqual(login_mock.call_args.args[0],
                         {"username": "u", "password": "p"})
        content = send_mock.call_args.args[1]
        self.assertIn("签到成功", content)
        # 账号密码登录返回的会话 cookie 用于签到 POST，不应泄露进通知
        self.assertNotIn("bbs_auth", content)

    def test_仅配置账号密码时登录签到(self):
        os.environ["LINUXSB_ACCOUNT"] = '{"username": "u", "password": "p"}'
        fake_session_cookie = "bbs_auth=authed"
        with fake_get(PAGE_UNCHECKED), fake_post({"ok": 1, "message": "好"}), \
             mock.patch.object(daily, "accounts_login",
                               return_value=fake_session_cookie) as login_mock, \
             mock.patch.object(daily.notify, "send") as send_mock:
            code = daily.run()
        self.assertEqual(code, 0)
        login_mock.assert_called_once()
        self.assertIn("签到成功", send_mock.call_args.args[1])

    def test_cookie失效且无凭据时明确报错(self):
        os.environ["LINUXSB_COOKIE"] = "a=1"
        with fake_get("<html>请登录</html>"), \
             mock.patch.object(daily, "accounts_login") as login_mock, \
             mock.patch.object(daily.notify, "send") as send_mock:
            code = daily.run()
        self.assertEqual(code, 1)
        login_mock.assert_not_called()
        self.assertIn("Cookie 已失效", send_mock.call_args.args[1])

    def test_cookie有效时走requests签到不启动登录(self):
        os.environ["LINUXSB_COOKIE"] = "a=1"
        os.environ["LINUXSB_ACCOUNT"] = '{"username": "u", "password": "p"}'
        with fake_get(PAGE_UNCHECKED), fake_post({"ok": 1, "message": "好"}), \
             mock.patch.object(daily, "accounts_login") as login_mock, \
             mock.patch.object(daily.notify, "send") as send_mock:
            code = daily.run()
        self.assertEqual(code, 0)
        login_mock.assert_not_called()
        self.assertIn("签到成功", send_mock.call_args.args[1])

    def test_无cookie无凭据时静默跳过(self):
        with mock.patch.object(daily.notify, "send") as send_mock, \
             mock.patch.object(daily, "accounts_login") as login_mock:
            code = daily.run()
        self.assertEqual(code, 0)
        send_mock.assert_not_called()
        login_mock.assert_not_called()


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
        """通知含各账号签到时间与概览；【linux.sb】域名不进通知"""
        import os

        os.environ["LINUXSB_COOKIE"] = "a=1"
        page = ('<div>当前积分：888</div><div>连续签到 3 天</div>'
                '<input type="hidden" name="_csrf" value="abc">')
        with fake_get(page), fake_post({"ok": 1, "message": ""}), \
             mock.patch.object(daily.notify, "send") as send_mock:
            code = daily.run()
        self.assertEqual(code, 0)
        content = send_mock.call_args.args[1]
        self.assertIn("签到时间: ", content)
        self.assertNotIn("【linux.sb】", content)
        self.assertNotIn("linux.sb", content)  # 站点域名不进入通知
        self.assertIn("账号 1", content)
        self.assertIn("签到结果: 签到成功", content)
        self.assertIn("当前积分: 888", content)
        self.assertIn("连续签到: 3 天", content)

    def test_日志不输出用户名只输出账号号(self):
        """用户名只进通知；日志（公开仓库 Actions 页面可见）只显示「账号 N」"""
        import os
        from unittest.mock import patch

        os.environ["LINUXSB_COOKIE"] = "bbs_auth=abc; bbs_csrf=csrf123"
        page = ('<a class="user-name" href="/user/42">秦昭襄王</a>'
                '<div>当前积分：888</div>'
                '<input type="hidden" name="_csrf" value="abc">')
        out = __import__("io").StringIO()
        with fake_get(page), fake_post({"ok": 1, "message": "好"}), \
             patch("sys.stdout", new=out), \
             mock.patch.object(daily.notify, "send") as send_mock:
            code = daily.run()
        self.assertEqual(code, 0)
        # 日志：有账号 1，无用户名、无 cookie 键名/值
        self.assertIn("账号 1", out.getvalue())
        self.assertNotIn("秦昭襄王", out.getvalue())
        self.assertNotIn("bbs_auth", out.getvalue())
        self.assertNotIn("bbs_csrf", out.getvalue())
        # 通知：用户名在，cookie 不在
        self.assertIn("秦昭襄王", send_mock.call_args.args[1])
        self.assertNotIn("bbs_auth", send_mock.call_args.args[1])

    def test_post响应redirect字段不展示(self):
        import os

        os.environ["LINUXSB_COOKIE"] = "a=1"
        with fake_get(PAGE_UNCHECKED), \
             fake_post({"ok": 1, "message": "", "redirect": "/daily_checkin", "bonus": 10}), \
             mock.patch.object(daily.notify, "send") as send_mock:
            daily.run()
        content = send_mock.call_args.args[1]
        self.assertNotIn("redirect", content)
        self.assertIn("bonus: 10", content)

    def test_签到前有随机延迟(self):
        import os

        os.environ["LINUXSB_COOKIE"] = "a=1"
        with fake_get(PAGE_UNCHECKED), fake_post({"ok": 1, "message": "好"}), \
             mock.patch.object(daily.notify, "send"):
            daily.run()
        self.sleep_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)