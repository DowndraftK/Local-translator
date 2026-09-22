# 流式字幕交付验证

2026-09-17 的完整报告见 [流式字幕与 TED 实测](../../docs/流式字幕与TED实测报告.md)。运行入口和资源准备见 [runtime](../../runtime/README.md)。

- [ted-results.json](ted-results.json)：两轮已完成流式 TED、录后 MLX、真实应用录后双语、PCM 管道输入的紧凑指标，不包含完整原文。
- [package-manifest.json](package-manifest.json)：0.2.1 构建 4 的应用/ZIP 路径、ZIP 哈希、应用内 Python 代码与工作区一致性的检查记录。
- `summarize_ted.py`：官方英文稿与完成会话的逐词编辑距离，以及模型估计的字幕延迟。拒绝把未完成会话计入整段结果。
- `summarize_delivery.py`：从完整证据生成不含原文的汇总。参数分别指定参考稿、多个 `--stream-session`、`--refined-session`、`--offline-result`、`--pcm-session`、`--output`。
- `check_pcm_input.py`：文件模拟麦克风的 stdin PCM 管道，等待模型准备后按不规则小包输入，校验原始字节、总样本数、完成状态和 EOF 尾句。不会打开实体麦克风。

真人视频与完整参考稿位于被忽略的 `artifacts/real-speech-20260916`。原始流式会话分别位于 `artifacts/streaming-translator-20260916/ted-mps-bilingual` 和 `artifacts/streaming-translator-20260917/ted-mps-context128`。应用创建的校对会话位于本机资料库，可在应用“打开已保存任务”找到。

管道复现示例（先准备运行配置；目标目录须不存在）：

```sh
PYTHONPATH=runtime PYTHONDONTWRITEBYTECODE=1 \
  artifacts/whisperlivekit-gpu-review-20260915/venv/bin/python \
  experiments/streaming-translator/check_pcm_input.py \
  --config artifacts/runtime.json --input /绝对路径/16k单声道.wav \
  --session artifacts/pcm-new-test
```

自动检查与真人推理是不同证据：Python 24 项、Swift 12 项通过，不意味着英文/中文质量或长课堂验收完成。中断的 1 秒合并试验明确排除于完整性能比较。应用构建曾与推理重叠，数值不应解读为严格隔离的硬件加速比。
