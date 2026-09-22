# WhisperLiveKit 在 Mac 上的 GPU 加速补充

日期：2026-09-15。上游仍固定在 `363e4f6d029694d9c81ae548beddd9d3c88a3637`，与 [技术评估](WhisperLiveKit技术评估与接入方案.md) 一致。

**后续更新（2026-09-16 整理）：** 下文是最初的随机权重算子探测与适配设计。随后已实际应用五文件实验补丁，用 large-v3-turbo 跑通 MLX GPU 编码、MPS GPU 解码和 SimulStreaming 提交。MPS FP32 重复轮接近 CPU 对照，但这段短录音未显示提速；完整证据、源码改动和复现命令见 [MPS 实测报告](WhisperLiveKit-MPS实测报告.md)。因此下文“优先适配”这一步已经完成，当前应保留为实验选项，再按实测决定集成与优化。

## 结论

**可以通过 PyTorch 的 MPS 后端使用 Mac GPU。当前 CPU 解码来自 WhisperLiveKit 的默认设备选择，不是 PyTorch 或 Mac 硬件只能使用 CPU。**

本轮已经在这台 M5 Pro 上验证：固定上游的 Whisper 小型随机权重解码器可将原始模型结构迁移到 `mps:0`，以 FP32 和 FP16 执行，缓存续写、交叉注意力、稀疏对齐头索引和中值滤波都通过检查。测试关闭了 `PYTORCH_ENABLE_MPS_FALLBACK`，没有把不支持算子自动回退 CPU 当成通过。

这证明 MPS 适配有实际基础。尚未运行完整 SimulStreaming 服务或真实 Whisper checkpoint，所以不能宣称已修好完整流式管线，也不能给出相对 CPU 的加速倍数。

## 1. 第一条路线：MLX 编码器 + PyTorch/MPS 解码器

MPS 是 PyTorch 调用 Apple Metal GPU 的后端。目标执行路径为：

```text
音频 → MLX 编码器（GPU）→ PyTorch 解码器（MPS/GPU）→ 流式文本提交
```

至少有两处设备选择需要贯通：

1. `whisperlivekit/simul_whisper/backend.py` 中，给 Whisper `load_model()` 显式传入 `device="mps"`。这个底层 loader 已有 device 参数，但现有后端调用没有传入。
2. `whisperlivekit/simul_whisper/simul_whisper.py` 中，`AlignAtt.__init__` 再次写死 `cuda if available else cpu`。应从已加载模型取得设备，让音频特征、状态张量和 decoder 权重保持一致。

核心修改方向如下，**这是适配设计，不是本轮已经打进上游的补丁，也不是现有 CLI 参数**：

```python
if not torch.backends.mps.is_available():
    raise RuntimeError("MPS GPU is unavailable")

# SimulStreamingASR.load_model() 内现有调用补 device 参数：
whisper_model = load_model(
    name=model_ref,
    device="mps",
    # 保留现有 decoder_only、模型路径及 alignment 参数
)

# AlignAtt.__init__ 内设备由传入模型决定：
self.device = loaded_model.device
```

正式实现还需：增加显式设备配置与日志；覆盖预热、常规解码、缓存、停顿、EOF、重置；确认模型和输入精度一致；验证模型加载与服务入口。先做 FP32 正确性，再比较 FP16 的质量和速度。不能用仅设置默认设备或打开 CPU fallback 代替这些修改。

本轮针对稀疏张量的检查有一项值得记录：较旧 PyTorch 的 MPS 稀疏限制不能直接套用到当前版本。**本机 PyTorch 2.14.0 下，未经结构修改的 `Whisper.to("mps")`、`alignment_heads.indices()` 及索引读取都通过了。** 最终探测代码没有把稀疏缓冲移回 CPU。

性能方面仍有两点需实测：

- MLX 与 PyTorch 都调用同一块 Apple GPU，当前特征转换还经过 NumPy；存在跨运行时同步与转换成本，不能等同于端到端单框架执行。
- 单词逐步生成的小计算有 GPU 调度开销；同时运行 Ollama 翻译还会竞争 GPU 和内存带宽。因此 GPU 可用不直接等于整条字幕链路必然快多少。

上游 [PR #383](https://github.com/QuentinFuxa/WhisperLiveKit/pull/383) 也记录过 MLX 输出在 MPS、decoder 权重在 CPU 的设备不匹配。该 PR 通过显式转换回 decoder 设备修复兼容性，没有将 decoder 改成 MPS。它解释了为何设备必须统一。

## 2. 第二条路线：WLK LocalAgreement + MLX Whisper

这条已有后端使用 `mlx_whisper.transcribe` 运行识别，编码器和解码器的主要模型计算可由 MLX 在 GPU 上执行，避免上述混合解码路径。

已准备完整 WLK 环境及 MLX 格式模型后，配置方向为：

```sh
wlk --backend mlx-whisper --backend-policy localagreement \
    --model_dir /绝对路径/已准备的MLX-Whisper模型 \
    --language en --pcm-input --host 127.0.0.1
```

固定版本使用的是 `--model_dir`（下划线）。此处模型路径是示意，不能指向现有 Core ML `.mlmodelc` 目录。整套离线运行还需准备 VAD 等资源，本轮没有执行这条服务启动命令。

优势是已有识别后端可以复用；代价是换成 LocalAgreement 提交策略，会反复识别滚动音频，再确认公共前缀。此前快速重复词去重问题仍需修正，首条与稳定文本延迟需要单独测量。

这与“修复全 MLX SimulStreaming”是不同路线。后者还涉及已有 MLX AlignAtt decoder 的标点后生成问题、配置入口与流式缓存验收，暂不作为最快的接入步骤。

## 3. 第三条路线：保留 WhisperKit 的 Core ML 硬件加速

本项目当前 `SpeechEngine` 没有强制 CPU 模式。Vendored WhisperKit 的 `ModelComputeOptions` 默认：

- Mel 特征：`.cpuAndGPU`。
- 当前 macOS 上的 audio encoder：`.cpuAndNeuralEngine`。
- text decoder：`.cpuAndNeuralEngine`。

这些是允许 Core ML 使用的计算单元配置，实际每层如何调度仍由运行时决定，不能据此声称所有计算都已在 Neural Engine。配置也允许使用 `.cpuAndGPU` 做对照，但无需仅为“使用 GPU”改掉可能更合适的 Neural Engine 路径。

因此，现有首条字幕约 16.54 秒的问题，不能解释为当前原生引擎缺少硬件加速。独立 15 秒窗口、稳定文本组装与翻译串行等待仍然需要修复。

## 4. 本机验证记录

硬件由 `system_profiler` 确认：Apple M5 Pro、20 个 GPU 核心、支持 Metal。隔离环境：Python 3.12.14、PyTorch 2.14.0；无训练模型下载。

| 检查 | 结果 |
| --- | --- |
| PyTorch 构建含 MPS | 是 |
| 可访问宿主 GPU 的进程中 MPS 可用 | 是 |
| 原始 Whisper 小型随机模型迁移到 MPS | 通过 |
| FP32 解码，权重、输出、KV 缓存位于 MPS | 通过 |
| FP16 解码，权重、输出、KV 缓存位于 MPS | 通过 |
| 稀疏 alignment_heads 保持在 MPS 并读取索引 | 通过 |
| 缓存续写与完整前向结果比较 | 通过 |
| 交叉注意力与 CPU FP32 参考比较 | 通过 |
| AlignAtt 所用中值滤波在 MPS 执行 | 通过 |

FP32 logits 相对 CPU FP32 最大绝对差约 `2.09e-7`；FP16 约 `4.20e-4`。这些仅是本次随机输入、两层 64 维解码器的数值差异，不是正式模型的准确率指标。

起初沙盒进程报告 `mps_available=false`，在获准访问宿主 GPU 后同一环境报告 true；这里是执行环境的可见性差异，不能解读为用户硬件不支持 MPS。

证据与复现：

- [探测脚本](../experiments/whisperlivekit/probe_mps.py)
- [本次机器结果](../experiments/whisperlivekit/mps-probe-result.json)
- [隔离环境依赖](../experiments/whisperlivekit/requirements-gpu-probe.txt)
- [实验说明](../experiments/whisperlivekit/README.md)

## 5. 更新后的实施建议

针对继续使用 WhisperLiveKit 的路线，**优先做显式 MPS decoder 适配，保留 SimulStreaming 提交策略；同时用现成 MLX/LocalAgreement 做对照。** 当前 WhisperKit 保留为原生基线。

采用判据仍是完整录音上的结果：英文稳定提交延迟、漏词与重复、条件句完整性、整句中文延迟、长期积压、功耗和并发翻译的影响。先完成短录音对照，再决定是否值得修复全 MLX SimulStreaming。
