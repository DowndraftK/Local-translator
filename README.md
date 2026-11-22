# 本地翻译器

本地翻译器是一款 macOS 应用，用于翻译文字、PDF、TXT 和图片，也可转写录音或在录音时显示双语字幕。文字识别使用 Apple Vision，语音识别使用 Whisper，翻译连接本机 Ollama。

当前交付版本为 **0.2.13 build143**，适用于运行 macOS 26.0 或更新版本的 Apple Silicon Mac。GitHub 仓库提供源码和测试，App 与 ZIP 安装包按本机交付。Windows 适配暂缓。

## 功能

| 功能 | 可以做什么 |
| --- | --- |
| 文字翻译 | 自选翻译方向和模型。翻译期间可继续编辑，取消时保留当前输入。 |
| 文档翻译 | 从 PDF 提取文字或直接读取 UTF-8 TXT，可翻译全文或指定页。原文可编辑，支持逐页跳转和双语对照。同一文档使用相同方向和模型再次提交时，可复用未改动页的译文。 |
| 图片翻译 | 保留原图预览，可编辑图下的识别文字。识别不到文字时可手动补录。 |
| 同声传译与录音转写 | 处理录音文件或选择麦克风、电脑声音作为同传来源。语音模型和翻译模型可分别选择。字幕带时间戳，录音可暂停、继续和停止。 |
| 词典与历史 | 离线英汉、汉英查词。在「我的翻译」查看已保存的原文和结果。 |

翻译可选英语、简体中文、日语、俄语、韩语、西班牙语和葡萄牙语。可用方向取决于当前模型。

## 开始使用

1. 解压本机交付的 ZIP，双击「本地翻译器.app」。
2. 使用翻译功能前，安装并启动 Ollama，准备至少一个本地文本生成模型。App 默认连接 `http://127.0.0.1:11434`，可在「偏好设置 → 本地服务」检测连接和查看模型清单。 [安装 Ollama](https://ollama.com/download/mac)
3. 在「偏好设置 → 通用」保存默认翻译方向和模型。初始方向为英语 → 简体中文，默认模型取 Ollama 清单中的首个本地模型。每次任务也可单独选模型。

## 语音资源

App 随包提供 WhisperLiveKit、Whisper small.en 英语模型、分词器、梅尔滤波器和语音活动检测（VAD）资源。要识别中文等其他语言，可在「偏好设置 → 语音资源」导入兼容的 MLX Whisper large-v3-turbo，再到同传或转写页选择它。

「语音资源」页可检查环境、导入受支持的模型目录、分词文件、辅助资源或运行组件，也可恢复内置资源。模型格式和固定版本见清单。 [small.en 清单](shared/translator_core/whisper-small.en.model.json) · [large-v3-turbo 清单](shared/translator_core/whisper-large-v3-turbo.model.json)

build143 已用合成音频验证英语转写、多语模型导入后的中文转写、同传暂停与继续。实体麦克风与电脑声音的权限和实际采集仍待本版复核。

## 开发与构建

桌面界面与打包入口位于 `prototype/b01`，翻译、任务存储和资源校验位于 `shared/translator_core`，macOS 系统适配位于 `shared/platform/macos`。 [桌面源码](prototype/b01/translator_preview) · [共享逻辑](shared/translator_core) · [macOS 适配](shared/platform/macos)

Mac 打包使用 Python 3.12.14、Qt/PySide6 6.10.2 和 Apple 命令行开发工具，依赖已校验的运行环境、语音模型、离线词典及许可材料。 [打包脚本](prototype/b01/build_preview.py) · [依赖锁定](prototype/b01/dependencies.lock.json) · [随包资源与许可](prototype/b01/mac-local-notices.md)
