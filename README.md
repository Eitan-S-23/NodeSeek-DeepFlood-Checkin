# NodeSeek 自动签到评论加鸡腿脚本

这是一个用于 NodeSeek 论坛的自动化脚本，包含签到、评论和加鸡腿功能。使用 Selenium 和 undetected-chromedriver 实现自动化操作。

强烈建议修改随机词。否则容易被举报被禁言。有能力的可以fork后自己定义改。

## 功能特点

- 自动签到（点击签到图标）
- 自动点击"试试手气"或"鸡腿 x 5"按钮（可配置）
- 随机选择帖子进行评论
- 自动给帖子加鸡腿（7天内的帖子）
- 随机评论内容（"bd"、"绑定"、"帮顶"）
- 除签到外的任务有一键开关，默认关闭
- 支持 GitHub Actions 自动运行
- 支持无头模式（可配置）
- 任务结束后推送通知到 Telegram 和企业微信

## 环境变量配置

### 基础配置

- `NS_COOKIE`: NodeSeek 的 Cookie（必需）
- `NS_RANDOM`: 是否随机选择奖励，true/false（可选，默认 false）
- `HEADLESS`: 是否使用无头模式，true/false（可选，默认 true）
- `NS_EXTRA_TASKS`: 除签到外的任务（评论、加鸡腿）总开关，true/false（可选，**默认 false**）
- `DEEPFLOOD_COOKIE`: DeepFlood 子站的 Cookie（可选）。配置后会自动追加签到第二站；两站用同一套代码、同样页面结构，仅域名与 cookie 不同
- `SITE_GAP_MIN` / `SITE_GAP_MAX`: 两站签到之间的随机延迟范围（秒，可选，默认 60-180），降低连续签到被风控的概率

布尔类型变量接受 `true`/`1`/`yes`/`on`/`y`（大小写不敏感）为真，其余值一律为假。

### 关于多站点签到

DeepFlood 是 NodeSeek 的子站，同一套论坛代码、同样的页面结构，只是独立域名与独立登录态。配置 `DEEPFLOOD_COOKIE` 后：

- 两站共用同一个浏览器实例，各自注入自己的 cookie 后签到，互不干扰
- 第二站开始前会先随机等待 `SITE_GAP_MIN`~`SITE_GAP_MAX` 秒，避免两次签到紧挨着被判定为机器批量行为
- 通知按站点分段显示，各带自己的等级与鸡腿数

### 关于 NS_EXTRA_TASKS

签到是无风险操作，始终执行。评论和加鸡腿会在他人帖子下留言，有被举报禁言的风险，因此单独用 `NS_EXTRA_TASKS` 控制，且默认关闭：

- 不配置或设为 `false`：只签到，不评论、不加鸡腿
- 设为 `true`：签到 + 评论 + 加鸡腿（建议先修改 `randomInputStr` 里的随机词）

### 通知配置（全部可选）

每次任务执行完毕后会汇总签到结果、评论数量、加鸡腿状态并推送。只配置哪个渠道就推送到哪个渠道，未配置的自动跳过，某个渠道失败不影响其他渠道，也不会导致任务失败。

Telegram：

- `TG_BOT_TOKEN`: Bot Token，从 [@BotFather](https://t.me/BotFather) 获取
- `TG_USER_ID`: 接收消息的用户或群组 ID
- `TG_API_HOST`: 自建反代地址（可选，默认 `https://api.telegram.org`）
- `TG_PROXY`: HTTP 代理地址（可选，例如 `http://127.0.0.1:7890`）

企业微信群机器人（配置最简单，推荐）：

- `WECOM_WEBHOOK`: 群机器人的 Webhook 完整地址（群聊 -> 添加群机器人 -> 复制 Webhook）

企业微信自建应用：

- `WECOM_CORPID`: 企业 ID
- `WECOM_CORPSECRET`: 应用的 Secret
- `WECOM_AGENTID`: 应用的 AgentId
- `WECOM_TOUSER`: 接收成员（可选，默认 `@all`）

通知内容示例（`NS_EXTRA_TASKS=true`）：

```
NodeSeek 每日任务

执行时间: 2026-07-29 08:05:12
签到结果: 签到成功，获得 5 个鸡腿
评论任务: 成功 18/20 个帖子
加鸡腿: 成功
```

只签到时（默认）：

```
NodeSeek 每日任务

执行时间: 2026-07-29 08:05:12
签到结果: 签到成功，获得 5 个鸡腿
附加任务: 已关闭（NS_EXTRA_TASKS 未开启）
```

## 本地运行

1. 克隆仓库
2. 安装依赖：`pip install -r requirements.txt`
3. 设置环境变量（可使用 .env 文件）
4. 运行脚本：`python nodeseek_daily.py`

## GitHub Actions 自动运行

1. Fork 本仓库
2. 在仓库的 Settings -> Secrets and variables -> Actions 中添加 Secret `NS_COOKIE`
3. 可选：添加 `NS_RANDOM` 设置是否随机选择奖励
4. 可选：需要评论和加鸡腿时，添加 `NS_EXTRA_TASKS=true`（不配置则只签到）
5. 可选：添加通知渠道的 Secrets（如 `WECOM_WEBHOOK` 或 `TG_BOT_TOKEN` + `TG_USER_ID`），workflow 已预置全部通知变量，未添加的自动跳过
6. Actions 会在每天 UTC 00:00（北京时间 08:00）自动运行，也可在 Actions 页面手动触发（workflow_dispatch）

`NS_EXTRA_TASKS` 和 `NS_RANDOM` 这类非敏感开关既可以放在 Variables 也可以放在 Secrets，workflow 会优先取 Variables。放在 Variables 的好处是能在页面上直接看到当前值。

## 注意事项

- 请确保 Cookie 有效且具有足够的权限
- 评论内容较为简单，开启 `NS_EXTRA_TASKS` 前建议先修改 `randomInputStr` 列表
- 加鸡腿功能仅对 7 天内的帖子有效
- 通知为可选功能，未配置任何渠道时脚本行为与之前完全一致
- 某个渠道推送失败只打印日志，不会影响签到结果和其他渠道
- GitHub Actions 中 Telegram 可能受网络限制，必要时用 `TG_API_HOST` 指向自建反代
- 定时任务的实际触发时间受 GitHub Actions 队列影响，通常会比 08:00 晚几分钟到几十分钟

## 测试

不启动浏览器即可运行全部单测：

```bash
python -m unittest discover -p "test_*.py" -v
```
