# 流式字幕交付验证

2026-10-01追加修复build21的短样本、波形锚点、模拟暂停/恢复和包检查完成，57分钟定向长测完成：五轮WER5.18%→2.05%、删除309→27词，本次旧循环位置未复现；818段中文收尾，音频/事件/导出完整。结果和限制见[追加记录](../../docs/0.2.6长会话与边界修复记录.md)及[speech-repair-20261001-results.json](speech-repair-20261001-results.json)。build18和09-17/09-23数字保留为历史证据，不回写成新版本已通过。

## 0.2.6 复现入口

- `inspect_quality_history.py --history artifacts/hardening-20260923/endurance-60m --output artifacts/new-history-slices`：只读旧SQLite/音频/日志，生成930–1060与3280–3385秒片段、原事件和样本映射；墙钟映射不当声音真值。
- `run_quality_trials.py --plan artifacts/new-quality-plan.json`：逐个串行执行真实模型，拒绝覆盖已有会话，记录退出码/完成状态；中断或ASR未完成不计通过。计划数组字段为session、input、config、paced、translation，可选runtime和pause_sample。使用新目录；暂停事件记录同样本95秒墙钟间隔，不插入假静音。所有GPU作业串行。
- `compare_quality.py --reference artifacts/reference.txt --session artifacts/completed-session --output artifacts/word-errors.json`：固定规范化、S/D/I、WER、最长连续删除、性能及模型估计提交延迟；完整操作明细只放忽略目录。
- `make_alignment_fixture.py --output artifacts/new-alignment-fixture`：本机已安装Daniel音色生成20句原创合成语音，推理前冻结幅值外沿锚点；需设置PYTHONPATH=runtime。不是真人听辨或设备验收。
- `check_alignment.py --anchors artifacts/new-alignment-fixture/anchors.json --session artifacts/completed-session --output artifacts/alignment-errors.json`：完整已完成会话评分，报告未匹配、有符号/绝对误差及所有超过2秒的锚点；不会只留成功匹配。`--spoken-forms`仅辅助小数/数字/Doctor缩写的时间对应，严格匹配结果另留，不改变WER口径。
- `evaluate_endurance.py --trial artifacts/completed-endurance --reference artifacts/reference.txt --output artifacts/assessment.json`：从同一只读事务获取完整行、事件与待组句尾部，按既有词规范化逐轮评分，检查事件重放、词序、时间回退/重叠、重复筛查和性能。默认拒绝未完成长测；`--allow-incomplete`仅生成标为未完成的中间观察。`--details`逐词错误必须留在忽略目录。相同录音重复不是独立材料，估计时间不是声学真值。
- `check_delivery.py --session artifacts/completed-session --output artifacts/delivery-checks.json`：核验全部数据库行、音频哈希、样本数、修订及三格式有效时间；格式完整不能代替声音对齐。

以上入口使用现有GPU环境中的Python；除夹具生成外，不下载/替换权重。固定补丁的重建和方法回归见[WhisperLiveKit实验说明](../whisperlivekit/README.md)。完整音频/参考稿/字幕/事件/数据库/日志留在artifacts，公开JSON仅保留紧凑指标。三种45秒输入及模拟暂停的文字差异、时间回退和歌词退化均见开发记录，不能用样本数一致宣称内容准确。


2026-09-17 的完整报告见 [流式字幕与 TED 实测](../../docs/流式字幕与TED实测报告.md)。运行入口和资源准备见 [runtime](../../runtime/README.md)。

- 2026-09-23 的恢复、60 分钟稳定性与纯标点修复证据见 [0.2.2 开发记录](../../docs/0.2.2录音与恢复开发记录.md) 及 [hardening-20260923-results.json](hardening-20260923-results.json)。本次 45 项 Python 回归通过，Swift 沿用 14 项证据；下面的 24/12 项和旧包清单均为 09-17 历史记录。
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
