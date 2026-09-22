# WhisperLiveKit：MLX 编码 + MPS 解码实测

实验日期：2026-09-15；报告整理：2026-09-16。对应需求：先尝试 **MLX 编码器（GPU）→ PyTorch/MPS 解码器（GPU）→ SimulStreaming 流式文本提交**。

后续进展（2026-09-17）：已接入原生应用、持久任务和独立中文队列，完成 11 分钟真人 TED 测试并加入录后 MLX 校对。最新实现与质量问题见 [流式字幕与 TED 实测](流式字幕与TED实测报告.md)。下文保留当时的隔离实验结论，不代表最新应用能力范围。

## 1. 结论

**这条路径已经使用真实的 Whisper large-v3-turbo 权重与真实 PCM 输入跑通。** 在本机 M5 Pro 上，MLX 使用 GPU，PyTorch decoder 权重及输入特征位于 `mps:0`，SimulStreaming 经 WhisperLiveKit 的 `AudioProcessor` 持续输出确认文本，并在 EOF 处理剩余音频与文本。

**本次短样本没有显示 MPS 解码的速度优势。** 第二轮 MPS FP32 的累计 ASR 调用耗时为 19.19 秒，CPU 解码对照为 18.34 秒；两轮都使用同一个 MLX GPU 编码器，端到端回放完成时间分别为 27.03 秒和 27.02 秒。它们都能在这段样本上跟随实时输入，并在输入结束后约 0.9 秒收尾。

本轮完成的是可运行的实验后端及实测。调用的是与 `/asr` 共用的进程内 PCM 处理管线，尚未启动 WebSocket 服务、接入 Swift 窗口、采集麦克风或生成中文。此前模型算子探测和本轮真实模型推理是两个阶段，证据范围不同。

## 2. 实际实现

固定上游：[QuentinFuxa/WhisperLiveKit，363e4f6](https://github.com/QuentinFuxa/WhisperLiveKit/commit/363e4f6d029694d9c81ae548beddd9d3c88a3637)，版本 `0.2.26`。

准备脚本先校验此前记录的源码指纹，再复制源码到独立实验目录，应用五个文件的修改：

| 文件 | 修改及用途 |
| --- | --- |
| `config.py` | 新增并验证 `decoder_device`、`decoder_dtype` |
| `parse_args.py` | 新增 `--decoder-device`、`--decoder-dtype`，允许显式选择 MPS/CPU 与 FP32/FP16 |
| `core.py` | 将配置传给 SimulStreaming 后端 |
| `simul_whisper/backend.py` | 加载模型时传入设备，预热前设置精度与推理模式；MPS 不可用时明确报错；识别异常向处理管线传播 |
| `simul_whisper/simul_whisper.py` | AlignAtt 从模型取得设备；编码特征同时匹配 decoder 的设备与精度 |

默认 `auto` 保留上游 CUDA/CPU 选择逻辑；本轮通过显式 `mps` 启用 Mac GPU。只有这份实验补丁新增了上述参数，未修改安装在其他位置的 WhisperLiveKit。

运行时设备证据：

```text
16 kHz 单声道 PCM，按真实时间每 0.5 秒送入
  → AudioProcessor / 语音活动检测与音频队列
  → MLX encoder：GPU，float16
  → 上游 NumPy 特征转换，再转换到 decoder 的 device / dtype
  → PyTorch decoder：mps:0，float32 或 float16
  → SimulStreaming / AlignAtt
  → FrontData.lines 确认文本，buffer_transcription 暂存文本
  → EOF 后排空结果流并检查尾部
```

所有推理运行在导入 PyTorch 前设置 `PYTORCH_ENABLE_MPS_FALLBACK=0`。MPS 配置下没有把不支持算子的自动 CPU 回退当作成功。音频预处理、Python 控制逻辑和 NumPy 转换仍涉及 CPU；这不是整条程序全在 GPU 上运行，也不是已实现 MLX/MPS 零拷贝。

脚本还确认 `uses_full_mlx_decoder=false`，所以没有误测成全 MLX decoder。模型创建、预热和队列中的推理共用一个 executor worker。

## 3. 模型、环境与计时方法

- 硬件：Apple M5 Pro，20 核 GPU；硬件信息沿用前一轮 `system_profiler` 记录。
- Python `3.12.14`、PyTorch `2.14.0`、MLX `0.32.2`、mlx-whisper `0.4.3`。
- 模型：[mlx-community/whisper-large-v3-turbo](https://huggingface.co/mlx-community/whisper-large-v3-turbo/tree/a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb)，固定 revision `a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb`。
- 下载 `config.json` 与约 1.61 GB 的 `weights.safetensors`。编码器与解码器复用同一份 checkpoint；WLK 原有加载逻辑将 MLX safetensors 转成 PyTorch decoder 权重，不另下载第二份模型。
- 使用该 turbo 模型的标准 alignment heads、英语、单 beam、SimulStreaming、0.5 秒输入包与合并阈值、VAD/VAC 开启、翻译和说话人区分关闭。
- 原样本为项目已有的 26.113 秒 Samantha 合成英语录音；重采样后为 417,809 个 16 kHz 样本。文本涉及申请截止、退款条件、统计显著性与因果关系。
- 每轮先加载模型并用录音前 3 秒预热，再启动音频回放计时。第一包在约 0.5 秒时送入；后续包按单调时钟的绝对时间发送。
- 后四轮均记录了运行前后 AC 电源状态；第一轮尚未记录电源状态。没有测量功耗或峰值内存。

三个时间概念必须分开：

1. **首个确认英文**：回放开始到第一次出现非空 `FrontData.lines.text`。本次该文本仅为 `Students`，不是完整英文句子或双语字幕。
2. **累计 ASR 调用耗时**：WLK 对识别调用的墙钟耗时求和，包含编码、转换、解码、对齐及线程调度。它不是单独的 decoder GPU 时间，也不包含等待下一包音频的全部时间。
3. **回放完成 / EOF 收尾**：前者从回放开始计算，后者从最后一包音频送入完成后计算，均排除模型加载与预热。

## 4. 短录音结果

下表全部使用同一段 26.113 秒录音、同一 MLX GPU encoder，仅改变 decoder 的设备或精度。

| Decoder 配置 | 首个确认英文 | 累计 ASR 调用耗时 | ASR 调用次数 | 回放完成 | EOF 后收尾 | 队列峰值音频 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| MPS FP32，首轮 | 1.328 s | 25.713 s | 41 | 27.751 s | 1.636 s | 1.458 s |
| MPS FP16 | 1.327 s | 22.551 s | 51 | 27.151 s | 1.036 s | 1.322 s |
| CPU FP32 对照 | 1.381 s | 18.336 s | 56 | 27.018 s | 0.902 s | 0.822 s |
| MPS FP32，重复轮 | 1.348 s | 19.190 s | 56 | 27.030 s | 0.914 s | 0.822 s |

模型加载加预热时间依次为 7.448、3.018、1.325、1.712 秒。这些是各轮观察值；没有清理系统缓存或控制编译缓存，不能当作严格冷启动对照。

**重复轮改变了对首轮差距的解释：MPS FP32 与 CPU FP32 的表现接近，尚无提速证据。** MPS FP32 重复轮的累计 ASR 调用时间约多 4.7%，回放完成时间只差约 0.012 秒。本样本长度、运行次数和计时控制不足以将这种差异概括为平台速度排名。

各轮输入包虽然相同，运行速度会影响队列合并和推理时实际看到的音频边界，ASR 调用次数因此不同。FP32 重复轮比首轮快，不足以单独证明是缓存的作用；FP16 单轮也不足以证明低精度更快或更慢。

### 文本与尾部

四轮均收到全部 417,809 个 PCM 样本，无处理错误或队列过载，最后的 `buffer_transcription` 为空。输入样本完整只证明管线接收完整，不等于每个词都识别正确。

首轮 MPS FP32 在转小写、忽略标点并把 `three` 归一为 `3` 后，与 69 词参考文本一致。其余三轮出现相同的一处差异：

```text
参考：The course is worth 3 credits.
输出：The courses worth three credits.
```

这处差异同时出现在 MPS FP32 重复轮、MPS FP16 和 CPU FP32，不能归因于 FP16 本身。最终输出缺少最后的句号；归一化比较不评估标点质量，也不等同于正式语料上的识别准确率。

四轮都保留完整退款条件句：

> A deposit of $250 is refundable only if the cancellation is received at least 14 days before the course begins.

最后的 `These findings do not establish a causal relationship` 也完整保留。这为连续处理退款条件和 EOF 尾句提供了短样本证据，但尚未测试中文译文。

## 5. 停顿、重复内容与 EOF

另构造了 **原录音 + 6 秒静音 + 原录音**，总长 58.226 秒，重采样后为 931,617 个样本，使用 MPS FP32 实测。

| 检查 | 结果 |
| --- | --- |
| 首个确认英文 | 1.351 秒 |
| 回放完成 / EOF 后收尾 | 59.026 秒 / 0.796 秒 |
| 实际收到样本数 | 931,617，与输入一致 |
| 开头 `Students must submit` | 出现两次 |
| 条件 `at least 14 days` | 出现两次 |
| 末尾 `causal relationship` | 出现两次 |
| 处理错误 / 过载 / 暂存尾部 | 无 / 无 / 空 |

两个实际重复段落均保留，长停顿后恢复输出，第二段末尾也成功提交。第一段仍有 `courses worth`，第二段识别为 `course is worth`。该测试没有覆盖快速连续重复词，也不替代此前 LocalAgreement 的 `very very` 失败用例。

模型给出的静音结束为 `32.26` 秒，第二段语音起点为 `32.04` 秒，存在约 0.22 秒交叠。后续用于点击回放或字幕导出时需处理估计时间戳的交叠；本轮未证明时间戳精确。

## 6. 对当前项目的意义

已解决本次尝试的核心可行性问题：**保留 SimulStreaming 时，可以在本机让 MLX encoder 与 PyTorch decoder 都使用 GPU，设备、精度、预热和 EOF 能在真实模型管线上贯通。** 六项 CLI/配置检查也通过。

尚未解决的产品问题包括完整句子的提交边界、中文延迟与质量、译文重试、原录音和任务持久化、麦克风输入、Swift 展示和长课堂积压。此次约 1.3 秒的首个英文单词不能与此前约 16.54 秒的首条双语片段直接计算加速倍数。

当前建议把 **MPS FP32 保留为可切换的实验选项和后续优化基线**。本轮数据不足以支持默认替换现有 WhisperKit，或声称 CPU decoder 在 Mac 上一定很慢。

若继续优化这条混合路线，优先定位两类成本：

- MLX→NumPy→PyTorch 的同步、转换与传递。当前代码确实经过该路径，可以先单独计时，再考虑有正确性验证的 DLPack 传递。
- Turbo 只有四层 decoder，逐 token 小计算及注意力读取的调度成本可能占比较高。需拆分编码、特征传递、解码与提交策略等待，才能判断 GPU 的收益被哪里抵消。

以上是可验证的优化假设，尚无算子级 profile 支持其占比。实际采用前，还要用同一段较长真人录音、相同文本提交策略，并发运行项目的 Ollama 翻译，比较首句中文、积压和错误率。

## 7. 代码、证据与复现

- [隔离源码准备与五文件补丁生成](../experiments/whisperlivekit/prepare_mps_trial.py)
- [真实模型流式运行器](../experiments/whisperlivekit/run_mps_trial.py)
- [结果验证与汇总脚本](../experiments/whisperlivekit/summarize_mps_trial.py)
- [完整结果摘要、文本差异、设备证据及原始记录哈希](../experiments/whisperlivekit/mps-trial-results.json)
- [模型 revision 与 SHA-256 清单](../experiments/whisperlivekit/mps-model-manifest.json)
- [实测环境依赖版本](../experiments/whisperlivekit/requirements-mps-trial.txt)
- [准备、重跑和复核命令](../experiments/whisperlivekit/README.md#真实模型的-mlx--mps--simulstreaming-实验)
- [本地生成的源码补丁](../artifacts/whisperlivekit-mps-20260915/mps.patch)

原始 `.json`、逐次 `.updates.jsonl` 和日志位于 `artifacts/whisperlivekit-mps-20260915/`，保留了首轮与重复轮记录。`artifacts/` 和模型目录默认不纳入 Git；摘要、脚本、固定版本清单和报告位于项目常规目录。

运行器设置本地模型路径、`HF_HUB_OFFLINE=1` 与 `TRANSFORMERS_OFFLINE=1`，本轮选定 ASR 路径使用快照中的 VAD 资源。尚未做物理断网冷启动验收。依赖环境按实际 ASR 路径安装；当时无法解析 `torchaudio==2.14.0`，未安装它，实际推理成功，因此该清单不代表验证了 WhisperLiveKit 所有可选后端。
