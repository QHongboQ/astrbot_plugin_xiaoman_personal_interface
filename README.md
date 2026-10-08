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

## 可选：林小满生命日规划器（v0.9.0）

默认关闭。启用前请安装并启用 `time_awareness`，并配置可用的 LLM provider。**林小满拥有最终的生命日时间线**；TimeAwareness 仅作为只读时钟、动态生命日边界、工作日/节假日、世界观、人设开关、主题/风格池和可用天气等上下文来源。林小满不会读取或依赖 TimeAwareness 日程快照，不调用它的 AI 日程生成器，也不会修改其源码、配置或快照。

每次生命日规划是一个全局 LLM 请求。v0.9.0 将语义规划与机械时间线结构分开：LLM 决定活动内容与顺序、每个自由窗内的相对持续时间权重、状态和播报文案；Python 按 `daily_schedule.ai_daily.generation_time` 确定性构造时间戳、NORMAL/BRIDGE 类型、时间线 ID、保护窗边界和完整无缝覆盖。Fat Fish 仅提供只读有效高峰政策；规划器按 `peak_guard_before_minutes` / `peak_guard_after_minutes` 扩展保护窗口，再把自由窗与保护窗交给 LLM。每个 Fxx 自由窗必须返回有序语义活动列表，每个 Pxx 保护窗必须返回一个 BRIDGE 语义对象。权重是窗内相对时长，不是概率；Python 保证正时长并按最大余数法分配分钟。

最终生成的 NORMAL / BRIDGE timeline 与 v0.8.x 持久化格式兼容，`SCHEMA_VERSION` 仍为 2，已有计划不会迁移或重写。对于缺失、空白或超限播报文案，Python 仅按已生成的活动名称提供确定性、有长度上限的兜底，不修改有效消息，也不会进行语义修复或额外 LLM 调用。最终时间线仍通过完整覆盖、无缺口/重叠、类别、边界及消息验证后保存到 Xiaoman 独立的 `life_day_plans.json`。刷新与到点投递均不调用 LLM；发送失败目标在宽限期内重试，成功目标不会重复发送。

AstrBot 4.28.2 的 `Context.llm_generate` 会将 per-call kwargs 交给 provider 的公开 `text_chat`，但内置 OpenAI adapter 不会把这些 kwargs 合入实际请求 payload。因此，小满不会传递无效的 per-call `response_format` / `max_tokens` 参数。若希望尽量降低完整日程输出被截断的概率，建议在 AstrBot WebUI 新建一个**专供 Xiaoman Planner 使用**的 OpenAI-compatible provider 实例（可以复用相同 API/model 凭据），并仅在该实例的 `custom_extra_body` 中配置：

```yaml
max_tokens: 8192
response_format:
  type: json_object
```

随后在小满配置页的 `provider_id` 中选用这个专用实例。不要把 JSON mode 配到日常聊天共用的 provider，否则普通对话也可能被强制要求输出 JSON。即使不做这项 provider 配置，小满仍使用紧凑输出提示、主题/风格/事件名称/状态硬长度限制、严格 JSON 解析和有界错误诊断；非法 JSON 或无效语义字段会安全失败，不会二次调用 LLM。缺失、空白或超长的投递文案则按已生成的事件名称作有界确定性兜底，不覆盖有效文案。

规划器提供用户可编辑的加权 `activity_pool` 和软性 `activity_density`。睡眠只是普通生活事件，用于自然节奏和避免全天高强度活动；Xiaoman 不按生理/医疗模型计算睡眠债、补偿时长或强制起床时间，也不会因为晚睡推导晚起。前一生命日历史仅提供轻量叙事与防重复线索，不会改变或占用 Fat Fish 保护窗。空闲窗内较长睡眠通常作为独立 `category=sleep` NORMAL；保护 BRIDGE 仍是一个不可拆分的顶层事件，若跨越睡眠、醒来和慢启动，通常用 `category=mixed`。新计划所有 timeline 项都必须包含允许值内的 `category`，已有 v0.8.0/v0.8.1 无类别计划仍兼容。

规划提示要求主题/风格忠实概括同一份最终时间线；普通事件按有意义的生活阶段组织，把买咖啡、通勤、换衣、洗澡、刷手机等支持动作通常并入较大的活动块。事件数按空闲窗长度和 `activity_density` 给软性参考，不机械拆分。Fat Fish 保护窗是规划结构，不是默认课程表；课程仍可安排，但两个保护窗不会仅因小满是大学生而自动变成早课和下午课。

所有 Xiaoman 自有的可调项都在插件齿轮配置页的“生命日规划与主动播报”组中：`enable`、`send_groups`、`send_private`、`allowlist_umos`、`denylist_umos`、`provider_id`、`peak_guard_before_minutes`、`peak_guard_after_minutes`、`activity_pool`、`activity_pool_allow_custom`、`activity_density`、`admin_regenerate_bypass_fat_fish`、`poll_seconds`、`grace_seconds`、`planner_failure_retry_seconds`、`max_message_chars`、`dry_run` 和 `planner_prompt`。活动池每项格式为 `活动名称,权重`，例如 `密室逃脱,7`；权重范围限制为 1–10，不写权重默认 1，权重只表示相对偏好。`activity_density` 可选 `relaxed`（约1–2项主要活动）、`balanced`（约2–3项）或 `busy`（约3–4项），均为软目标。即使时间线完整覆盖24小时，也允许长时间休息、睡觉、宅家或无安排；睡眠时长和起床时间由规划器自由选择，不建模睡眠债或必须起床时间。只使用 AstrBot 已有会话，不枚举 QQ 群/好友。旧的 `schedule_broadcast_state.json` 会保留为 legacy 文件，不会被误当成 v0.8 生命日计划迁移。

三个插件配置页各自是其所属设置的唯一来源：

- Xiaoman 齿轮配置控制小满自己的目标筛选、发送范围、保护分钟数、活动偏好/密度、规划重试、文案限制、dry-run 和管理员手动重规划开关；睡眠时长不作为配置或硬约束。
- TimeAwareness 齿轮配置控制生命日边界 `daily_schedule.ai_daily.generation_time`、worldview、人设开关、主题/风格池、天气与近期历史窗口（`adaptive.recent_days` / continuity）；Xiaoman 只读这些上下文，不复制成自己的配置。
- Fat Fish 齿轮配置控制 `enabled`、timezone、高峰时段/星期、provider 范围、`manual_override` 和 `admins_bypass`；Xiaoman 通过 v1.1.1 的公开 `fish.config` 只读这些值，不调用私有方法、不修改配置。

仅在自动模式、启用且 provider 受影响，并且 TimeAwareness 当日类型为工作日或调休工作日时生成保护窗；周末/节假日、`always_allow` 和 `always_block` 都不生成自动高峰保护窗。管理员显式执行 `regenerate` 时，只有 Xiaoman 的 `admin_regenerate_bypass_fat_fish` 与 Fat Fish 的 `admins_bypass` 同时开启才可绕过钱包高峰拦截；这只影响该次手动规划，启动补全、后台 tick、次日预生成及直接服务调用始终遵守 Fat Fish 拦截。绕过时会记录不含用户隐私的 INFO 审计日志。Fat Fish 管理员绕过开关由 Fat Fish 配置负责，Xiaoman 不复制该设置。

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

`status` 展示生命日边界、有效高峰/保护窗/自由窗、当前与下一生命日计划状态、下一条播报和最近规划错误，也显示保护分钟数、两侧管理员绕过开关和 dry-run。`raw cycle`/`plan` 查看 Xiaoman 权威时间线。`regenerate` 才会重新调用规划 LLM；`refresh` 只从已有时间线重建投递项；`simulate` 使用隔离状态走真实发送路径，遵守名单设置；`test` 只测试指定已生成消息。`dry_run` 不自动发送，管理员明确运行 `simulate` 时允许一次真实投递。

## 管理员测试模式

仅 AstrBot 管理员 UID `979675497` 可使用 `/xiaoman_test on|off|status`。模式按 UMO 会话隔离、仅保存在内存中，插件重启后关闭；启用期间只对当前管理员会话提供测试放行 guidance，并在该事件中隔离 AngelHeart。

## 安装与验证

在 AstrBot 插件页安装本仓库 Release 的 ZIP，或将本目录放入 AstrBot 插件目录后重启/重载插件。

```powershell
python -m compileall -q main.py services tools tests
python -m unittest discover -s tests -v
python tests/runtime_smoke.py
```
