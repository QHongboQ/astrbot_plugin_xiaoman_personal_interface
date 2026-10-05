# 林小满个人接口

一个面向 AstrBot `>=4.28.2` 的林小满稳定能力适配插件。

目前包含两个独立能力：

1. Photo Capability：`send_xiaoman_photo()`。
2. Voice Performance Guidance：在主 LLM 请求前追加稳定的林小满 MiMo 表演规则，让回复可在自然需要时包含隐藏的表演标签。

它注册无参数工具：

```text
send_xiaoman_photo()
```

该工具仅供已经依据人格和对话上下文决定发送照片的上层 LLM 调用。用户提出照片请求本身不构成调用条件。

语音表演规则不调用额外模型、不改写人格或 TTS 配置；它只提供稳定的格式与使用原则。实际的标签处理、语音合成和展示文字清理仍由现有 TTS 插件负责。

## 依赖与行为

需要已启用并已注册 `gallery_send` 工具的 Airi Gallery v2.11.15。

- 工具始终以固定目标发送一张林小满照片。
- 目标工具不可用或调用抛出异常时，返回 `PHOTO_SEND_FAILED`。
- 仅在委托工具明确确认已发送一张目标照片时返回 `PHOTO_SENT`；其他普通返回同样返回 `PHOTO_SEND_FAILED`。
- 不会选择其他分类，也不会实现图片存储、随机选择或发送逻辑。
- 不负责 TTS 合成，不修改 TTS 插件，不决定是否发送语音，不负责语音概率或 VoiceClone。
- 不负责人格、记忆、好感度或其他关系判断。

## 安装

在 AstrBot 插件页上传本仓库 Release 附带的 ZIP 安装包，或将本目录放入 AstrBot 的插件目录后重启/重载插件。

启用 Airi Gallery 的 LLM 工具后，再启用本插件。

## 开发验证

```powershell
python -m unittest discover -s tests -v
```
