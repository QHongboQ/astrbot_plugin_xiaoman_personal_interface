# 林小满个人接口

一个面向 AstrBot `>=4.28.2` 的极简 LLM Function Tool 插件。

它注册无参数工具：

```text
send_xiaoman_photo()
```

该工具仅供已经依据人格和对话上下文决定发送照片的上层 LLM 调用。用户提出照片请求本身不构成调用条件。

## 依赖与照片行为

需要已启用并已注册 `gallery_send` 工具的官方 Airi Gallery v2.11.15。

- 工具始终以固定目标发送一张林小满照片。
- 目标工具不可用、调用抛出异常或未确认发送时，返回 `PHOTO_SEND_FAILED`。
- 仅在委托工具明确确认已发送一张目标照片时返回 `PHOTO_SENT`。
- 不会选择其他分类，也不会实现图片存储、随机选择或发送逻辑。

每次 LLM 请求中，插件会检查当前 `ToolSet`：仅当其中同时存在 `send_xiaoman_photo` 与 `gallery_send` 时，才从该请求的工具集合移除 `gallery_send`。这样 LLM 只看到封装后的照片工具，而封装工具仍可通过 AstrBot 全局工具管理器调用 `gallery_send`。此操作不会更改全局注册表或 Airi 插件；如果封装工具未暴露或请求工具集不兼容，则保持 fail-open，不添加工具、不隐藏其他工具。

## 自然对话与图库命令

自然语言由正常 LLM 对话处理。用户提出看照片的请求不自动触发工具；是否调用 `send_xiaoman_photo()` 完全由 LLM 根据人格和上下文决定。插件不分类或改写自然语言消息，也不拦截图库事件。

图库浏览命令由 Airi Gallery 独立处理。将 Airi 的 `view_command_mode` 配置为 `prefix` 后，显式命令使用 `/`，例如 `/看看小满`、`/看看默认`、`/看看123`、`/看100-110` 和 `/看全部小满`。普通聊天文本如 `看看小满` 不匹配 Airi 的前缀浏览语法，会留在正常 LLM 流程。

职责边界：自然对话 → LLM 决定是否调用 Xiaoman 工具 → 固定委托 `gallery_send(category="林小满", count=1)`；显式 `/看...` 浏览命令 → Airi Gallery。

本版本不包含任何 TTS、语音提示、表演标签或 MiMo 逻辑。

## 可选：TimeAwareness 日程广播（v0.6.0）

默认关闭。启用前请安装并启用官方 `time_awareness`。Xiaoman 仅经该插件的运行时服务注册指定会话 Persona（`trigger=False`）、读取已经存在的日程快照，并由 TimeAwareness 自己的详情 API 提供合并后的有效时间线；不读取其文件/数据库，不手动合并 AI、用户、静态与已执行层，也不会触发 TimeAwareness 日程生成。没有可用快照时不广播。

快照变化后，Xiaoman 对未来且非空的时段进行一次批量消息生成，使用当前默认 Persona prompt；到点时只发送已保存消息，不调用 LLM。消息投递记录按 UMO 持久化，失败目标可在宽限期内重试，成功目标不重复发送。

配置项：`enable`、`schedule_source_umo`（日程 Persona 所属的现存会话；留空使用第一个合格目标）、`send_groups`、`send_private`、`allowlist_umos`、`denylist_umos`、`provider_id`、`event_offset_minutes`、`poll_seconds`、`grace_seconds`、`max_message_chars`、`dry_run` 与 `broadcast_prompt`。仅使用 AstrBot 已有会话，不枚举 QQ 群/好友。

### Fat Fish 策略兼容

Xiaoman 为官方 Fat Fish 1.1.1 提供自己的钱包策略接口，并仅对活动实例安装可逆的运行时 `_cfg("manual_override")` 包装。TimeAwareness 是工作日/假日唯一来源：假日和周末在自动模式视为非高峰；调休工作日继续走 Fat Fish 原有高峰规则；TimeAwareness 不可用/未知时不臆造节假日。`always_block` 和 `always_allow` 优先级不变。

兼容桥仅在内存中包装活动实例 `_cfg("manual_override")` 的读取，不改 Fat Fish 配置文件或已保存的 `manual_override`，卸载时仅在 wrapper 仍由 Xiaoman 持有时恢复。Fat Fish 官方文件与 TimeAwareness 官方文件均不修改。

管理员命令：`/xiaoman_broadcast status` 查看运行诊断，`refresh` 读取快照并按变化生成，`test` 预览已生成消息。状态不会显示密钥或插件配置全文。

管理员命令：

```text
/xiaoman_broadcast status
/xiaoman_broadcast refresh
/xiaoman_broadcast test
```

`status` 查看服务状态；`refresh` 立即重新读取日程（相同哈希不重复花费生成调用）；`test` 只在当前管理员会话预览或发送一条已经生成的待发送消息，且不会把正式日程标记为已发送。`dry_run` 模式下 `test` 只预览。

解析支持形如 `08:55｜地点：学校｜事项：上午课程｜细节：今天第一节课有点困` 的中英文管道符格式；错误行会跳过。日程内容是事实来源，本插件不会向对话历史写入主动消息。

## 管理员测试模式

仅 AstrBot 管理员 UID `979675497` 可使用 `/xiaoman_test on|off|status`。模式按 UMO 会话隔离、仅保存在内存中，插件重启后关闭；启用期间只对当前管理员会话提供测试放行 guidance，并在该事件中隔离 AngelHeart。

## 安装与验证

在 AstrBot 插件页安装本仓库 Release 的 ZIP，或将本目录放入 AstrBot 插件目录后重启/重载插件。

```powershell
python -m compileall -q main.py services tools tests
python -m unittest discover -s tests -v
python tests/runtime_smoke.py
```
