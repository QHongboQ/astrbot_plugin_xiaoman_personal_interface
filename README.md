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

## 可选：林小满生命日规划器（v0.8.0）

默认关闭。启用前请安装并启用 `time_awareness`，并配置可用的 LLM provider。**林小满拥有最终的生命日时间线**；TimeAwareness 仅作为只读时钟、动态生命日边界、工作日/节假日、世界观、人设开关、主题/风格池和可用天气等上下文来源。林小满不会读取或依赖 TimeAwareness 日程快照，不调用它的 AI 日程生成器，也不会修改其源码、配置或快照。

每次生命日规划是一个全局 LLM 请求：先依据 `daily_schedule.ai_daily.generation_time` 生成从边界时刻到下一日同一边界的完整时间线，并在同一次响应中生成主题、风格和所有 NORMAL/高峰桥接消息。Fat Fish 仅提供只读有效高峰政策；规划器按 `peak_guard_before_minutes` / `peak_guard_after_minutes` 扩展保护窗口，再把完整自由窗与保护窗一起交给规划器。保护窗恰好对应一个 BRIDGE 活动。计划通过无缺口、无重叠及消息完整性验证后，保存到 Xiaoman 独立的 `life_day_plans.json`。刷新与到点投递均不调用 LLM；发送失败目标在宽限期内重试，成功目标不会重复发送。

启用配置包括 `enable`、`provider_id`、`peak_guard_before_minutes`（默认5）、`peak_guard_after_minutes`（默认5）、`send_groups`、`send_private`、`allowlist_umos`、`denylist_umos`、`poll_seconds`、`grace_seconds`、`max_message_chars`、`dry_run` 和可选的 `planner_prompt`。只使用 AstrBot 已有会话，不枚举 QQ 群/好友。旧的 `schedule_broadcast_state.json` 会保留为 legacy 文件，不会被误当成 v0.8 生命日计划迁移。

Fat Fish 通过其公开 `get_wallet_policy()` 只读接口提供 enabled、manual override、provider 范围和高峰配置。仅在自动模式、启用且 provider 受影响，并且 TimeAwareness 当日类型为工作日或调休工作日时生成保护窗；周末/节假日、`always_allow` 和 `always_block` 都不生成自动高峰保护窗。Xiaoman 不包装或替换 Fat Fish `_cfg`，不改 Fat Fish 配置。

管理员命令：

```text
/xiaoman_broadcast status
/xiaoman_broadcast raw cycle
/xiaoman_broadcast plan current|next
/xiaoman_broadcast regenerate current|next|cycle
/xiaoman_broadcast refresh
/xiaoman_broadcast simulate HH:MM
/xiaoman_broadcast reset current|next
/xiaoman_broadcast test <entry_id>
```

`status` 展示生命日边界、有效高峰/保护窗/自由窗、当前与下一生命日计划状态、下一条播报和最近规划错误。`raw cycle`/`plan` 查看 Xiaoman 权威时间线。`regenerate` 才会重新调用规划 LLM；`refresh` 只从已有时间线重建投递项；`simulate` 使用隔离状态走真实发送路径，遵守名单设置；`test` 只测试指定已生成消息。`dry_run` 不自动发送，管理员明确运行 `simulate` 时允许一次真实投递。

## 管理员测试模式

仅 AstrBot 管理员 UID `979675497` 可使用 `/xiaoman_test on|off|status`。模式按 UMO 会话隔离、仅保存在内存中，插件重启后关闭；启用期间只对当前管理员会话提供测试放行 guidance，并在该事件中隔离 AngelHeart。

## 安装与验证

在 AstrBot 插件页安装本仓库 Release 的 ZIP，或将本目录放入 AstrBot 插件目录后重启/重载插件。

```powershell
python -m compileall -q main.py services tools tests
python -m unittest discover -s tests -v
python tests/runtime_smoke.py
```
