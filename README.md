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

## 可选：Life Scheduler 日程广播（v0.3.1）

此功能默认关闭。启用后，插件只通过 AstrBot 已加载插件元数据查找名称精确为 `astrbot_plugin_life_scheduler`、已激活且实例可用的 Life Scheduler，并调用其公开的 `get_life_context(allow_generate=False)`。它只读取已经生成的日程，不导入或读写 Life Scheduler 的文件，也不触发日程生成。日程文本发生变化（包括重写）时，插件解析有效节点并对整份计划进行一次批量消息生成；发送时不调用 LLM。消息使用 AstrBot 默认人格的 `prompt`，再附加 gear 中可编辑的广播指令。

在插件配置齿轮的“林小满日程广播”组中设置：

- `enable`：开启服务（默认关闭）。
- `target_mode`：`all_conversations` 只表示 AstrBot 已存在的群/私聊会话；`allowlist` 仅使用允许列表。
- `send_groups` / `send_private`：分别控制群聊与私聊；拒绝列表始终优先。
- `provider_id`：指定生成提供商；留空时从首个目标会话解析。
- `event_offset_minutes`：事件触发时间偏移，可用 `-5` 提前五分钟。
- `poll_seconds`、`grace_seconds`、`max_message_chars`：轮询、过期宽限期与消息长度上限。
- `dry_run`：解析并生成计划、记录到日志，但绝不广播。
- `broadcast_prompt`：只填写广播风格补充，不覆盖 Xiaoman 的默认人格。

计划仅保存在本插件数据目录的 `life_broadcast_state.json`，使用原子替换写入。重启后相同日程复用已生成消息；已发送项目不会重复发送，过期项目不补发。广播只投递至 AstrBot 已有会话，不读取 QQ 好友/群列表，也不写入聊天历史。

高峰期策略完全由已启用的 Fat Fish Wallet (`astrbot_plugin_fat_fish_wallet`) 控制。Xiaoman 只通过 AstrBot 插件元数据发现实例并调用其公开的 `get_wallet_policy()`；不读取插件配置副本、不解析时段/星期，也不重复节假日或强制覆盖逻辑。必须安装并启用带有该公开策略接口的 Fat Fish 版本，否则不会生成或发送主动广播。Fat Fish 返回的当前策略用于生成前保护、每个未来事件过滤、JSON 修复前检查及实际发送前复查。中国法定节假日由 Fat Fish 在自动模式中按非高峰处理。插件状态命令直接显示 Fat Fish 返回的启用状态、时区、覆盖模式、节假日及名称、当前策略、提供商适用性和允许结果。

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
