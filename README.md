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

## 定向消息路由保护

官方 Airi Gallery 的无前缀浏览功能会处理以“看”开头的文本。若消息已由 AstrBot 标记为直接唤醒林小满（@、唤醒词或适用的私聊），并且规范化后的文本以“看”开头，本插件只会从**当前事件**已激活处理器列表中原地移除 Airi 的 `handle_gallery_message`。

这让 `@小满 看看你的照片`、`@小满 看看林小满` 等定向自然语言请求继续进入正常 LLM 流程；是否调用照片工具仍完全由 LLM 决定。未直接唤醒林小满的 `看看林小满`、`看看123`、`看100-110` 等无前缀 Airi 命令保持原行为。

路由保护不会停止事件、不会改写消息文本或组件、不会导入或修改 Airi，也不会影响 `gallery_send` 工具。

本版本不包含任何 TTS、语音提示、表演标签或 MiMo 逻辑。

## 安装与验证

在 AstrBot 插件页安装本仓库 Release 的 ZIP，或将本目录放入 AstrBot 插件目录后重启/重载插件。

```powershell
python -m compileall -q main.py services tools tests
python -m unittest discover -s tests -v
```
