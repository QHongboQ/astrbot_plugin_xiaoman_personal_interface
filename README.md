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

## 管理员测试模式

仅 AstrBot 管理员 UID `979675497` 可使用 `/xiaoman_test on|off|status`。模式按 UMO 会话隔离、仅保存在内存中，插件重启后关闭；启用期间只对当前管理员会话提供测试放行 guidance，并在该事件中隔离 AngelHeart。

## 安装与验证

在 AstrBot 插件页安装本仓库 Release 的 ZIP，或将本目录放入 AstrBot 插件目录后重启/重载插件。

```powershell
python -m compileall -q main.py services tools tests
python -m unittest discover -s tests -v
```
