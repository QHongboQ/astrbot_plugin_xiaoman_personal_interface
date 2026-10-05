# 林小满个人接口

一个面向 AstrBot `>=4.28.2` 的极简 LLM Function Tool 插件。

它注册无参数工具：

```text
send_xiaoman_photo()
```

该工具仅供已经依据人格和对话上下文决定发送照片的上层 LLM 调用。用户提出照片请求本身不构成调用条件。

## 依赖与行为

需要已启用并已注册 `gallery_send` 工具的 Airi Gallery v2.11.15。

- 工具始终以固定目标发送一张林小满照片。
- 目标工具不可用或调用抛出异常时，返回 `PHOTO_SEND_FAILED`。
- 仅在委托工具明确确认已发送一张目标照片时返回 `PHOTO_SENT`；其他普通返回同样返回 `PHOTO_SEND_FAILED`。
- 不会选择其他分类，也不会实现图片存储、随机选择或发送逻辑。

## 安装

在 AstrBot 插件页上传本仓库 Release 附带的 ZIP 安装包，或将本目录放入 AstrBot 的插件目录后重启/重载插件。

启用 Airi Gallery 的 LLM 工具后，再启用本插件。

## 开发验证

```powershell
python -m unittest discover -s tests -v
```
