# 本地翻译器

仅供本人使用的原生 Mac 离线翻译器。功能规划在 [产品与技术规划](docs/产品与技术规划.md)。

当前交付为 **0.2.2 原生 Mac 录音与恢复测试应用**和命令行验证程序。应用提供文字翻译、文档/图片提取及选段翻译、麦克风/文件连续字幕、任务保存与补译、录后重新校对、本机资源设置。完整首版仍需阅读、文档、实时质量及长课堂恢复等全部通过验收。

2026-09-22 新增 VAD 前置音频、独立录音落盘、暂停/继续、检查点恢复和字幕分页。进展与待验收项见 [0.2.2 开发记录](docs/0.2.2录音与恢复开发记录.md)。

2026-09-17 已将固定的 WhisperLiveKit 接入主应用，新增 SQLite 持久任务、英文先保存和独立中文队列。使用官方 TED 真人录音（11 分 20 秒）完成长音频对照；流式存在明显漏词，完整录后 MLX 识别的词错误率显著降低。实测结果和局限见 [流式字幕与 TED 实测报告](docs/流式字幕与TED实测报告.md)，使用及恢复说明见 [runtime/README.md](runtime/README.md)。

[剩余开发规划与解决方案](docs/剩余开发规划与解决方案.md) 保留完整首版工作包，并新增本轮 T01/T03/T04/T05/T06/T09 等的部分实施状态。文档阅读、术语、资料库、真实麦克风与长课堂等尚未全部完成。

实时使用 **MLX GPU 编码 + PyTorch/MPS GPU 解码 + SimulStreaming 提交**。早期短样本的 MPS 解码未比 CPU 更快，历史证据见 [MPS 实测报告](docs/WhisperLiveKit-MPS实测报告.md)。录后校对使用同一份权重的完整 MLX 推理，生成独立版本；这项速度不能等同于实时延迟。

## 打开图形界面

当前应用位于 [本地翻译器.app](dist/M0-20260922-214708-release/本地翻译器.app)，压缩包位于 [本地翻译器-M0.zip](dist/M0-20260922-214708-release/本地翻译器-M0.zip)。在 Finder 中双击应用即可。它面向当前 Apple Silicon Mac，要求 macOS 26 或更新，采用本机临时签名，未做公开分发或 Developer ID 公证。

| 页面 | 已接入的交互 |
| --- | --- |
| 文字翻译 | 粘贴、示例、英中方向选择、模型选择、翻译、复制及保存对照 |
| 文档与图片 | 选择文件、提取方式与页数、片段列表、来源标记、选段翻译、打开原文件及保存本段对照 |
| 录音翻译 | 音频/视频导入、麦克风输入、英文先保存、独立中文、任务打开/补译、片段回放/纠错、录后校对、TXT/SRT/VTT |
| 本机资源 | 查看本机模型、检查服务、启动仅本地 Ollama、重新选择语音资源目录 |

模型继续使用本机已有的文件，不装入应用或 ZIP。应用中预设了当前项目 `models` 目录；若移动模型，请在“本机资源”重新选择。若 Ollama 未运行，可点击“启动本地服务”。应用只停止自己启动的服务，不停止原先已经运行的 Ollama。

新的录音任务保存在 `~/Library/Application Support/LocalTranslator/Recordings`；翻译失败或关闭后可打开任务补译。识别中断可继续处理已保存音频，或生成独立校对版。实体麦克风已完成环境音采集及暂停/继续检查；设备拔插、真实休眠、自然课堂质量与完整长时验收的状态见开发记录。文字/文档和“原生引擎对照”仍使用原有临时目录。

本包已包含 Python 运行代码，但流式功能还依赖项目内的 Python 环境、WhisperLiveKit 补丁源码和 MLX 权重；请保留对应 `artifacts` 与 `models`，具体位置见 [运行环境说明](runtime/README.md)。它目前是本机自用包，并非独立安装发行版。

重新生成应用与 ZIP：

```sh
bash scripts/package-app.sh
```

脚本执行 Release 构建，将界面和工作进程、图标及依赖声明打包并验证签名，每次写入新的 `dist/M0-时间-release/` 目录。图形界面通过独立工作进程调用已验证的 `translator-m0`，模型加载不阻塞窗口。原有命令行入口和测试仍然保留。

## 使用已下载的模型开始测试

本机已接入 `hy-mt2:1.8b-q8`、`hy-mt2:7b-q8`，以及 Whisper `small.en`、`large-v3-turbo` Core ML 资源。两套分词器缺失的 `tokenizer.json` 已补齐。模型推理使用本地文件，不依赖 Apple Intelligence。

若 Ollama 尚未运行，在一个终端启动仅本地服务（保持该终端运行）：

```sh
bash scripts/ollama-local.sh
```

该脚本只设置当前进程的本机监听、关闭云功能和单模型驻留，不修改全局配置。若端口已经被占用，先确认现有服务配置，不必重复启动。

在另一个终端运行完整的短样本集成测试：

```sh
bash scripts/m0-smoke.sh
# 或者用你自己的短录音替换合成音频：
bash scripts/m0-smoke.sh /绝对路径/英语录音.m4a
```

脚本编译程序，比较两种 HY-MT2 的双向翻译，生成语音资源校验清单，比较两套 Whisper，再运行 turbo + 1.8B 的录音双语处理和按时长回放测试。默认样本由已安装的 Samantha 离线语音生成，不使用麦克风。结果分别保存到 `artifacts/smoke-时间-进程号/`，重复运行不会覆盖上一次结果。此测试不会下载模型；缺资源或服务不可用会停止报错。

## 编译与基础检查

本轮已在当前机器更新后的 Swift 6.4 / macOS 27 SDK 上构建，最低部署系统仍为 macOS 26。依赖源码已固定在 Vendor 中，构建不下载模型或远程 Swift 包。SwiftPM 输出目录由 `--show-bin-path` 确定，不再假定固定为 `.build/debug`。

在项目目录运行：

```sh
bash scripts/swift.sh build
bash scripts/swift.sh test
translator_bin="$(bash scripts/swift.sh build --show-bin-path)"
"$translator_bin/translator-m0" --help
"$translator_bin/translator-m0" doctor --output artifacts/doctor.json
```

脚本将编译缓存放进项目 `.build`，不要求改动用户全局缓存设置。脚本禁用 SwiftPM 的额外包清单沙盒，外层系统权限仍然有效。

## 文字翻译

先显式启动配置为仅本地模式的 Ollama，并准备选定模型。程序只接受回环地址、检查已安装模型元数据、拒绝云模型标记和 HTTP 重定向；它不会自动启动服务或下载模型。

```sh
"$translator_bin/translator-m0" translate --input fixtures/translation-en.txt --model hy-mt2:1.8b-q8 --direction en-zh --output artifacts/translation.json
"$translator_bin/translator-m0" translate --input fixtures/translation-zh.txt --model hy-mt2:7b-q8 --direction zh-en --output artifacts/translation-zh-en.json
```

`--model` 必须与本地服务列出的模型名一致。可用 `--glossary 文件.json` 提供 `[ { "source": "credit", "target": "学分" } ]`。报告保存译文、模型、首字/完成耗时、输出 token 数和数字/术语核对提示。提示不是准确率或确定性误译判定。

`hy-mt2:` 名称自动采用腾讯模型卡的用户消息格式、术语提示和 1.8B/7B 推荐采样参数，再加入本项目的保真要求；仅向声明支持思考的模型发送 `think: false`。报告记录提示方案、模型加载、输入计算和生成耗时。API 计时从发出生成请求开始，不包含之前的模型元数据检查；“首次请求”不等于操作系统缓存全冷。推荐参数温度为 0.7，重复运行的译文可能不同。限制提示词不能保证模型不误译，尤其须人工检查条件、期限和量词。

M0 单次限制为 8000 字符；当前不自动翻译整本书，不具备完整上下文预算和跨段术语一致性保障。只有正常结束的响应才输出成功记录；缺少完成标记、输出上限截断、空译文和服务错误均报错。

## 文档提取

```sh
"$translator_bin/translator-m0" extract --input fixtures/sample.docx --output artifacts/docx-extraction.json
"$translator_bin/translator-m0" extract --input fixtures/sample.pptx --output artifacts/pptx-extraction.json
"$translator_bin/translator-m0" extract --input /绝对路径/样本.pdf --mode text --pages 10 --output artifacts/pdf-text.json
"$translator_bin/translator-m0" extract --input /绝对路径/样本.pdf --mode ocr --pages 10 --output artifacts/pdf-ocr.json
"$translator_bin/translator-m0" extract --input /绝对路径/样本.pdf --mode layout --pages 10 --output artifacts/pdf-layout.json
```

支持 TXT/MD、PDF、DOCX、PPTX 和常见图片的提取探针。PDF 分别比较文字层、Vision OCR 和 Vision 文档结构识别；默认只处理前 10 页，可显式调整。结构识别的原始坐标、表格等保存到 `layoutEvidenceJSON`，方便核对，并未完整映射到最终数据模型。

DOCX 当前提取正文和表格段落；PPTX 按 presentation.xml 关系确定真实页序。未完成列表编号、合并单元格、原版渲染、Office 图片 OCR 和全部特殊对象。每份结果包含限制提示。此命令仅提取，不会把提取不完整的文件自动宣称为翻译完成。

`fixtures/sample.docx` 和 `sample.pptx` 是用于提取测试的合成包，不是 Office 视觉兼容性样本。

## WhisperKit 原生引擎对照（旧路径）

下面仅说明保留的 `translator-m0` 对照入口；新的流式字幕、持久任务和录后校对请使用 [runtime](runtime/README.md)。下文关于独立窗口和未接入功能的限制属于旧路径。

资源需要包括已编译的 `AudioEncoder.mlmodelc`、`TextDecoder.mlmodelc`、`MelSpectrogram.mlmodelc` 以及匹配的 `tokenizer.json`、`tokenizer_config.json` 等文件。不得用 Ollama 的 GGUF 权重代替 WhisperKit 的 Core ML 模型。

```sh
"$translator_bin/translator-m0" seal-speech --model-folder /绝对路径/语音模型 --tokenizer-folder /绝对路径/分词器 --output artifacts/speech-resources.json
"$translator_bin/translator-m0" transcribe --input /绝对路径/录音.m4a --resources artifacts/speech-resources.json --model hy-mt2:1.8b-q8 --output artifacts/speech.json
"$translator_bin/translator-m0" replay --input /绝对路径/录音.m4a --resources artifacts/speech-resources.json --model hy-mt2:1.8b-q8 --output artifacts/replay.json
```

`seal-speech` 建立文件哈希清单，忽略 `.cache` 等隐藏的下载元数据；后续加载先校验，缺失或损坏直接失败。程序使用固定依赖中的严格本地分词器补丁，说明见 [Vendor/README.md](Vendor/README.md)。本机模型的实际加载和合成语音推理已开始验证，真实课堂质量与完整断网测试仍待完成。

`transcribe` 不传 `--model` 时只测英文转写，传入则测转写加中文翻译。`replay` 按音频时长逐段输入，用来测持续处理和落后时长；它不是已实现的麦克风实时字幕。

语音报告分别记录 `resourceLoadSeconds`（哈希校验和 Core ML/分词器加载）、`elapsedSeconds`（音频处理及片段检查点保存）、`totalElapsedSeconds`（加载加处理，最终 JSON 写入前计时）。`processingSeconds` 是当前音频窗口开始处理后、此片段完成时的累计耗时，不能逐项相加。回放的 `lagSeconds` 使用模型估计的片段结束时间，不能代替真实麦克风端到端延迟验收。

当前采用独立的约 15 秒音频窗口，仅做近乎数字静音的过滤，尚未完成课堂 VAD、跨窗口去重、稳定英文提交、麦克风采集、暂停恢复和最终 SRT/VTT 导出。边界可能漏词或重复，报告明确提示。完成的片段会保存到 `.partial.json`，但尚未实现自动读取该文件续跑。

## 验证完成的含义

编译通过、合成样本通过、模型推理通过、真实材料质量通过、断网及长时运行通过是不同的结果。M0 要逐项完成后才能退出，不以代码存在或测试数量代替翻译质量。

最新实测及待办见 [M0验证进展](docs/M0验证进展.md)。`artifacts` 中的结果可能含原文或录音内容，默认不纳入 Git。
