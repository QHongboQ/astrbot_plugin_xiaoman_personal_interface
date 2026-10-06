# 林小满个人接口

一个面向 AstrBot `>=4.28.2` 的林小满稳定能力适配插件。

目前包含两个独立能力：

1. Photo Capability：`send_xiaoman_photo()`。
2. Voice Tool Adapter：仅在现有 `tts_speak` 工具实际调用前，为未带表演标签的文本添加基线标签。

## 照片能力

`send_xiaoman_photo()` 仅供已经依据人格和对话上下文决定发送照片的上层 LLM 调用；用户提出照片请求本身不构成调用条件。

照片能力需要已启用并已注册 `gallery_send` 工具的 Airi Gallery v2.11.15。

- 始终以固定目标发送一张林小满照片。
- 工具不可用、调用异常或未确认发送时，返回 `PHOTO_SEND_FAILED`。
- 仅确认发送后返回 `PHOTO_SENT`。
- 不会选择其他分类，也不会实现图片存储、随机选择或发送逻辑。

## 语音工具适配

v0.1.4 不会向正常 LLM 请求注入任何语音指令，因此正常的林小满文字回复不受影响。

仅在公开的 `tts_speak` 工具实际被调用时，适配器才检查其 `text` 参数：若没有已知表演标签，则添加基线标签：

```text
（语速稍快，连续说，停顿很短）
```

已有的全角或 ASCII 括号表演标签保持不变，避免相邻标签堆叠。适配器不做自动情绪选择、不调用额外模型，也不修改 TTS 配置。

本插件不负责 TTS 合成，不修改 TTS 插件，不决定是否发送语音，不负责语音概率或 VoiceClone；也不负责人格、记忆、好感度或其他关系判断。

## 安装与验证

在 AstrBot 插件页安装本仓库 Release 的 ZIP，或将本目录放入 AstrBot 插件目录后重启/重载插件。

```powershell
python -m compileall -q main.py services tools tests
python -m unittest discover -s tests -v
```
