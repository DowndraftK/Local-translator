# 本地翻译器 0.2.14 build146 · 语音资源导入

适用于 Apple Silicon、macOS 26.0 或更新版本。解压 ZIP 后双击「本地翻译器.app」。在「偏好设置 → 语音资源」查看四种预设。模型、分词器和配套文件从本机目录导入。翻译连接本机已安装的 Ollama，默认地址是 `http://127.0.0.1:11434`。

## 模型与用途

| 预设 ID | 固定仓库与修订 | 权重 | 这个 App 接收的原文 | 同传 | 录音转写 | 录后精修 |
| --- | --- | --- | --- | --- | --- | --- |
| `small.en` · Whisper small.en | `mlx-community/whisper-small.en-fp16` · `f8ff44ec66c4b1748fb2e3eb13b3b521a0bdfea8` | MLX Safetensors，FP16，约 481 MB | 英语 | 英语试音通过 | 英语试音通过 | 英语试音通过 |
| `large-v3-turbo` · Whisper large-v3-turbo | `mlx-community/whisper-large-v3-turbo` · `a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb` | MLX Safetensors，FP16，约 1.61 GB | 英语、简体中文、日语、俄语、韩语、西班牙语、葡萄牙语 | 英语试音通过 | 英语试音通过 | 英语试音通过 |
| `qwen3-asr-0.6b` · Qwen3-ASR 0.6B | `Qwen/Qwen3-ASR-0.6B` · `5eb144179a02acc5e5ba31e748d22b0cf3e303b0` | 官方 Safetensors，BF16，约 1.88 GB | 英语 | 待导入后验证 | 待导入后验证 | 本版未开放 |
| `qwen3-asr-1.7b` · Qwen3-ASR 1.7B | `Qwen/Qwen3-ASR-1.7B` · `7278e1e70fe206f11671096ffdd38061171dd6e5` | 官方 Safetensors，BF16，约 4.70 GB，两个分片 | 英语 | 待导入后验证 | 待导入后验证 | 本版未开放 |

[Whisper small.en 固定来源](https://huggingface.co/mlx-community/whisper-small.en-fp16/tree/f8ff44ec66c4b1748fb2e3eb13b3b521a0bdfea8) · [Whisper large-v3-turbo 固定来源](https://huggingface.co/mlx-community/whisper-large-v3-turbo/tree/a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb) · [Qwen3-ASR 0.6B 固定来源](https://huggingface.co/Qwen/Qwen3-ASR-0.6B/tree/5eb144179a02acc5e5ba31e748d22b0cf3e303b0) · [Qwen3-ASR 1.7B 固定来源](https://huggingface.co/Qwen/Qwen3-ASR-1.7B/tree/7278e1e70fe206f11671096ffdd38061171dd6e5)

两款 Whisper 共用随包的语音运行组件 `0.2.10-r5`，WhisperLiveKit 固定在 `363e4f6d029694d9c81ae548beddd9d3c88a3637`。同传和文件转写走现有 MLX 编码与 MPS FP32 解码，精修走 MLX Whisper。权重精度以表中 FP16 为准。[WhisperLiveKit 固定源码](https://github.com/QuentinFuxa/WhisperLiveKit/tree/363e4f6d029694d9c81ae548beddd9d3c88a3637)

官方 Qwen 模型还支持其他语言与方言。本版 Metal 路径只开放英语验证。社区 MLX 8-bit 权重和 Qwen 录后精修均待各自后端实测。[Qwen3-ASR 模型说明](https://huggingface.co/Qwen/Qwen3-ASR-0.6B/blob/5eb144179a02acc5e5ba31e748d22b0cf3e303b0/README.md)

## 准备目录并导入

1. 把固定修订的实际文件放在本机普通目录中。Qwen 的 `config.json`、权重和分片索引要位于所选目录的这一层。Hugging Face 缓存里的符号链接应先物化成普通文件。
2. 在相应预设点「导入模型目录…」。展开「文件与来源」可看固定版本、所需文件和具体缺项。
3. 点「补齐资源」，选取单个分词或辅助文件，也可选包含多项的目录。补齐时使用下列原始文件名。Qwen 的分词与预处理文件可以随权重目录一起导入，也可分开补齐。
4. 资源齐备后显示「待验证」。点该预设的「本机验证」。App 用包内短试音加载实际后端，检查适用模式的英语输出，并在同传检查连续 PCM、停止前的稳定字幕和停止后的尾句。通过的模式显示「可用」。各模式的失败信息在「文件与来源」中。
5. 前往「同传转写」，选择「同声传译」或「录音转写」，再选原文语言、已通过对应验证的识别模型与本机 Ollama 模型。启动中的任务固定识别模型、权重 SHA-256、后端版本和翻译配置。切换选择用于下一次开始。在同传停止后，可从已封存录音和历史查看原文、译文与时间戳。

**Whisper 模型目录**

```text
whisper-small.en/
  config.json
  model.safetensors

whisper-large-v3-turbo/
  config.json
  weights.safetensors
```

两款 Whisper 各自需要以下 `speech-data-v1` 文件。如本机已保存 `speech-runtime-data-v1`，可在「补齐资源 → 选取辅助资源目录」选择该目录。手动准备时，使用固定 WhisperLiveKit 修订里的 `whisper/assets` 和 `silero_vad_models` 对应文件。两处 `assets` 下的三个同名文件要求内容一致。App 可以在导入单个同名文件时核对并填充两个位置。[WhisperLiveKit 分词与梅尔滤波目录](https://github.com/QuentinFuxa/WhisperLiveKit/tree/363e4f6d029694d9c81ae548beddd9d3c88a3637/whisperlivekit/whisper/assets) · [WhisperLiveKit VAD 目录](https://github.com/QuentinFuxa/WhisperLiveKit/tree/363e4f6d029694d9c81ae548beddd9d3c88a3637/whisperlivekit/silero_vad_models)

```text
speech-runtime-data-v1/
  wlk/whisper/assets/
    gpt2.tiktoken
    multilingual.tiktoken
    mel_filters.npz
  mlx/assets/
    gpt2.tiktoken
    multilingual.tiktoken
    mel_filters.npz
  wlk/silero_vad_models/
    silero_vad.onnx
    silero_vad_16k_op15.onnx
    silero_vad.jit
    silero_vad_half.onnx
```

**Qwen 模型目录**

```text
Qwen3-ASR-0.6B/                      Qwen3-ASR-1.7B/
  config.json                         config.json
  model.safetensors                   model-00001-of-00002.safetensors
                                      model-00002-of-00002.safetensors
                                      model.safetensors.index.json
```

两款 Qwen 各自目录都需要下面六个分词、模板及预处理文件。

```text
chat_template.json
generation_config.json
merges.txt
preprocessor_config.json
tokenizer_config.json
vocab.json
```

导入按大小和固定内容校验。Safetensors 检查 SHA-256，Qwen 的小文件核对固定 Git blob。Qwen 1.7B 还核对分片索引和实际张量归属。App 把核验通过的文件复制到托管目录，完成后发布新选择。取消、错型号、漏分片和损坏文件导致导入终止时，原先已选目录可继续使用。

## Qwen 独立运行包

请准备 `qwen3-metal-mac-arm64-v1` 目录，在语音资源页底部点「Qwen 独立运行包 → 导入运行目录…」。两款 Qwen 共用此目录。App 使用自带的 CPython 3.12.14 标准库和独立的 Qwen `site-packages`，该解释器以 `-I -S` 启动。识别时使用 WhisperLiveKit 的 `qwen3-vllm-metal`、官方 BF16 权重和标准声学编码路径。

```text
qwen3-metal-mac-arm64-v1/
  component.json
  site-packages/
    ...Apple Silicon CPython 3.12 的运行依赖...
```

固定主版本如下。准备 wheelhouse 时使用与这组版本兼容的依赖，最终运行目录清单记录所有实际安装的版本和文件摘要。

| 运行组件 | 版本 |
| --- | --- |
| vLLM Metal | `0.33.0` · CPython 3.12 arm64 |
| vLLM | `0.33.0+cpu` · macOS arm64 |
| Qwen adapter package | `qwen3-asr-causal 0.1.0` · WhisperLiveKit 源码固定修订对应适配 |
| MLX | `0.32.4` |
| Transformers | `5.20.0` |

[vLLM Metal 固定 release](https://github.com/vllm-project/vllm-metal/releases/tag/v0.33.0) · [vLLM macOS CPU wheel](https://github.com/vllm-project/vllm/releases/download/v0.33.0rc3/vllm-0.33.0%2Bcpu-cp38-abi3-macosx_11_0_arm64.whl) · [Qwen adapter 0.1.0](https://pypi.org/project/qwen3-asr-causal/0.1.0/) · [上游依赖声明](https://github.com/vllm-project/vllm-metal/blob/v0.33.0/pyproject.toml) · [上游 PyTorch 组件版本](https://github.com/vllm-project/vllm-metal/blob/v0.33.0/vllm_metal/requirements-torch.txt)

将这些 wheel 及其依赖放到一个本地 wheelhouse，每个依赖保留一个适用于 arm64 的版本。上游依赖声明中的 MLX-LM 和 MLX-VLM 使用固定 Git 修订，wheelhouse 应包含由对应修订构建的 wheel。由本机已有的 `uv` 解包和封存，命令只读 wheelhouse，新建输出目录。

```sh
APP="/应用所在目录/本地翻译器.app"
WHEELS="/本机目录/qwen-wheelhouse"
OUTPUT="/本机目录/qwen3-metal-mac-arm64-v1"
"$APP/Contents/Resources/Runtime/whisper/runtime/Python/bin/python3.12" -I -B \
  "$APP/Contents/Resources/B01/prepare_qwen_runtime.py" "$WHEELS" "$OUTPUT"
```

导入 `OUTPUT` 后，分别对 0.6B 与 1.7B 点「本机验证」。完整正式权重与 Metal 依赖实测通过时，各自显示「同传 可用」「录音转写 可用」。打开同传，以英语作为原文语言，通过麦克风或电脑声音读入包含数字、否定、专名和独立尾句的短句。暂停后继续，最后在尾句一结束时点「停止」。查看稳定字幕、Ollama 中文、封存录音和最后一句。Qwen 字幕时间使用 PCM 窗口内的词位置估计。Whisper 使用模型对齐给出的时间。

## 从 build143 升级

App 继续使用 `~/Library/Application Support/LocalTranslator/Qt`。早期已导入且匹配固定清单的 Whisper 目录可以沿用；首次使用时可重新运行本机验证。此前使用随包权重或辅助文件的预设显示「待导入」。旧 App 可以留在原处，旧任务可从「我的翻译」打开。build146 的新图标已在 Finder 和解包副本中确认。
