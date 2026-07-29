# -- coding: utf-8 --
"""主脚本配置解析与通知正文测试。

nodeseek_daily 依赖 selenium / undetected_chromedriver 等浏览器库，
本地与 CI 的单测环境不一定安装，这里用桩模块替换后再导入，
使开关逻辑与正文拼装可以脱离浏览器独立验证。
"""
import sys
import types
import unittest
from unittest import mock


def _install_stub_modules():
    """为浏览器相关依赖注册最小桩模块，仅满足 import 期需要的属性访问。"""
    if "nodeseek_daily" in sys.modules:
        return

    def stub(name, **attrs):
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules.setdefault(name, module)
        return module

    class _Anything:
        """任意属性访问都返回自身，覆盖 By.XPATH、EC.xxx 之类的用法。"""

        def __getattr__(self, item):
            return self

        def __call__(self, *args, **kwargs):
            return self

    stub("undetected_chromedriver", Chrome=_Anything(), ChromeOptions=_Anything())
    stub("bs4", BeautifulSoup=_Anything())

    stub("selenium")
    stub("selenium.webdriver")
    stub("selenium.webdriver.common")
    stub("selenium.webdriver.common.by", By=_Anything())
    stub("selenium.webdriver.common.keys", Keys=_Anything())
    stub("selenium.webdriver.common.action_chains", ActionChains=_Anything())
    stub("selenium.webdriver.support")
    stub("selenium.webdriver.support.ui", WebDriverWait=_Anything())
    stub("selenium.webdriver.support.expected_conditions", presence_of_element_located=_Anything())


_install_stub_modules()

import nodeseek_daily as daily  # noqa: E402  桩模块必须先安装


class EnvBoolTestCase(unittest.TestCase):
    """校验布尔环境变量解析，避免出现 NS_RANDOM="false" 被判为真的老问题。"""

    def test_真值写法全部识别为真(self):
        for raw in ("true", "TRUE", "True", "1", "yes", "on", "y", " true "):
            with mock.patch.dict("os.environ", {"NS_TEST_FLAG": raw}):
                self.assertTrue(daily.env_bool("NS_TEST_FLAG"), f"{raw!r} 应为真")

    def test_假值写法全部识别为假(self):
        for raw in ("false", "FALSE", "0", "no", "off", "随便写"):
            with mock.patch.dict("os.environ", {"NS_TEST_FLAG": raw}):
                self.assertFalse(daily.env_bool("NS_TEST_FLAG"), f"{raw!r} 应为假")

    def test_未设置或空串时取默认值(self):
        with mock.patch.dict("os.environ", {"NS_TEST_FLAG": ""}):
            self.assertFalse(daily.env_bool("NS_TEST_FLAG"))
            self.assertTrue(daily.env_bool("NS_TEST_FLAG", default=True))

        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(daily.env_bool("NS_TEST_FLAG"))
            self.assertTrue(daily.env_bool("NS_TEST_FLAG", default=True))


class ChromeVersionTestCase(unittest.TestCase):
    """校验 Chrome 大版本解析，驱动版本必须与浏览器一致否则无法建立会话。"""

    def test_解析标准版本输出(self):
        self.assertEqual(daily.parse_chrome_major_version("Google Chrome 150.0.7871.128"), 150)
        self.assertEqual(daily.parse_chrome_major_version("Chromium 151.0.1.2"), 151)

    def test_无法解析时返回None(self):
        for raw in ("", None, "no version here"):
            self.assertIsNone(daily.parse_chrome_major_version(raw), f"{raw!r} 应返回 None")

    def test_环境变量可覆盖探测结果(self):
        with mock.patch.dict("os.environ", {"CHROME_MAJOR_VERSION": "149"}):
            self.assertEqual(daily.detect_chrome_major_version(), 149)

    def test_环境变量非数字时忽略并继续探测(self):
        with mock.patch.dict("os.environ", {"CHROME_MAJOR_VERSION": "abc"}), \
                mock.patch("shutil.which", return_value=None):
            self.assertIsNone(daily.detect_chrome_major_version())


class BuildNotifyContentTestCase(unittest.TestCase):
    """校验通知正文在三种结果下的表述。"""

    SIGN_OK = {"success": True, "detail": "签到成功，获得 5 个鸡腿"}

    def test_附加任务关闭时说明已关闭且不输出统计(self):
        content = daily.build_notify_content(self.SIGN_OK, None)
        self.assertIn("签到成功", content)
        self.assertIn("已关闭", content)
        self.assertNotIn("0/0", content)
        self.assertNotIn("加鸡腿", content)

    def test_附加任务开启时输出评论与鸡腿统计(self):
        stats = {"total": 20, "commented": 18, "chicken_leg": True, "error": ""}
        content = daily.build_notify_content(self.SIGN_OK, stats)
        self.assertIn("成功 18/20 个帖子", content)
        self.assertIn("加鸡腿: 成功", content)

    def test_评论异常时正文体现异常原因(self):
        stats = {"total": 0, "commented": 0, "chicken_leg": False, "error": "TimeoutException 超时"}
        content = daily.build_notify_content({"success": False, "detail": "签到失败"}, stats)
        self.assertIn("异常终止", content)
        self.assertIn("TimeoutException", content)
        self.assertIn("加鸡腿: 未成功", content)


class RunTestCase(unittest.TestCase):
    """校验 NS_EXTRA_TASKS 开关真正决定评论任务是否被调用。"""

    SIGN_OK = {"success": True, "detail": "签到成功"}

    def test_开关关闭时不调用评论任务(self):
        with mock.patch.object(daily, "extra_tasks_enabled", False), \
                mock.patch.object(daily, "setup_driver_and_cookies", return_value=object()), \
                mock.patch.object(daily, "nodeseek_comment") as comment, \
                mock.patch.object(daily, "click_sign_icon", return_value=self.SIGN_OK), \
                mock.patch.object(daily.notify, "send") as send:
            code = daily.run()

        comment.assert_not_called()
        self.assertEqual(code, 0)
        self.assertIn("已关闭", send.call_args.args[1])

    def test_开关开启时调用评论任务(self):
        stats = {"total": 20, "commented": 20, "chicken_leg": True, "error": ""}
        with mock.patch.object(daily, "extra_tasks_enabled", True), \
                mock.patch.object(daily, "setup_driver_and_cookies", return_value=object()), \
                mock.patch.object(daily, "nodeseek_comment", return_value=stats) as comment, \
                mock.patch.object(daily, "click_sign_icon", return_value=self.SIGN_OK), \
                mock.patch.object(daily.notify, "send") as send:
            code = daily.run()

        comment.assert_called_once()
        self.assertEqual(code, 0)
        self.assertIn("20/20", send.call_args.args[1])

    def test_浏览器初始化失败时推送失败通知且不执行任务(self):
        with mock.patch.object(daily, "extra_tasks_enabled", True), \
                mock.patch.object(daily, "setup_driver_and_cookies", return_value=None), \
                mock.patch.object(daily, "nodeseek_comment") as comment, \
                mock.patch.object(daily, "click_sign_icon") as sign, \
                mock.patch.object(daily.notify, "send") as send:
            code = daily.run()

        comment.assert_not_called()
        sign.assert_not_called()
        self.assertEqual(code, 1)
        self.assertIn("失败", send.call_args.args[0])

    def test_签到失败时退出码为1(self):
        with mock.patch.object(daily, "extra_tasks_enabled", False), \
                mock.patch.object(daily, "setup_driver_and_cookies", return_value=object()), \
                mock.patch.object(daily, "click_sign_icon",
                                  return_value={"success": False, "detail": "签到失败: 超时"}), \
                mock.patch.object(daily.notify, "send") as send:
            code = daily.run()

        self.assertEqual(code, 1)
        self.assertIn("签到异常", send.call_args.args[0])


class ShouldSkipCookieTestCase(unittest.TestCase):
    """校验 cookie 过滤：环境绑定与统计类 cookie 必须跳过，登录态必须保留。"""

    def test_跳过_cloudflare_与统计类_cookie(self):
        for name in ("cf_clearance", "__cf_bm", "__cflb", "_ga", "_ga_47LDR1H8FC", "_gid", "_gat"):
            self.assertTrue(daily.should_skip_cookie(name), f"{name} 应跳过")

    def test_保留登录态相关_cookie(self):
        for name in ("session", "pjwt", "smac", "fog", "colorscheme"):
            self.assertFalse(daily.should_skip_cookie(name), f"{name} 应注入")

    def test_大小写与空白不影响判定(self):
        self.assertTrue(daily.should_skip_cookie("  CF_Clearance  "))
        self.assertFalse(daily.should_skip_cookie("  Session  "))


class ParseCookieStringTestCase(unittest.TestCase):
    """校验 NS_COOKIE 解析：正常项注入、CF 项跳过、值含分号不被截断、多行粘贴容错。"""

    def _names(self, pairs):
        return [name for name, _ in pairs]

    def test_基本分号分隔并跳过cf项(self):
        pairs, _ = daily.parse_cookie_string("session=abc; cf_clearance=xyz; smac=123")
        self.assertEqual(self._names(pairs), ["session", "smac"])

    def test_值中含分号不被截断(self):
        # 某个 cookie 的值本身含分号（如被截断的 JSON），后半段应拼回而非产生残缺片段
        pairs, skipped = daily.parse_cookie_string("session=a;b;c; smac=1")
        self.assertEqual(self._names(pairs), ["session", "smac"])
        session_value = dict(pairs)["session"]
        self.assertEqual(session_value, "a;b;c")
        # 不应因值里的分号报告异常片段
        self.assertEqual(skipped, [])

    def test_换行作为分隔符(self):
        pairs, _ = daily.parse_cookie_string("session=abc\nsmac=123\r\npjwt=xyz")
        self.assertEqual(self._names(pairs), ["session", "smac", "pjwt"])

    def test_跳过原因不含cookie值(self):
        _, skipped = daily.parse_cookie_string("cf_clearance=secretvalue; session=x")
        joined = " ".join(skipped)
        self.assertIn("cf_clearance", joined)
        self.assertNotIn("secretvalue", joined)

    def test_空输入返回空列表(self):
        pairs, skipped = daily.parse_cookie_string("")
        self.assertEqual(pairs, [])
        self.assertEqual(skipped, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
