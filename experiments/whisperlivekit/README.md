# WhisperLiveKit 研究验证

实验日期：2026-09-15；更新：2026-09-16。最初结论见 [技术评估与接入方案](../../docs/WhisperLiveKit技术评估与接入方案.md)，后续真实模型结果见 [MPS 实测报告](../../docs/WhisperLiveKit-MPS实测报告.md)。

这里保留最初的隔离研究测试。2026-09-17 原生应用已通过 [runtime](../../runtime/README.md) 接入固定的 MPS 补丁源码，新增独立中文队列和任务持久化。最初的控制逻辑测试使用固定上游源码、合成 token 与假翻译函数，无 ASR/MT 模型加载；后续增加了 GPU 算子探测和真实 Whisper 模型流式实验。各阶段的环境与证据分开记录。

## 固定来源

- 仓库：<https://github.com/QuentinFuxa/WhisperLiveKit>
- 提交：`363e4f6d029694d9c81ae548beddd9d3c88a3637`
- `pyproject.toml` 版本：`0.2.26`
- 源码包 SHA-256：`12320732d23f4890a71e375212c52c1ee92047cc3697c53fa3209b74f52423f1`
- [review-metadata.json](review-metadata.json) 记录关键源码指纹、环境和本轮结果。

## 在当前工作区重跑无模型测试

从项目根目录执行（快照与虚拟环境已准备）：

```sh
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  artifacts/whisperlivekit-review-20260915/venv/bin/python -m pytest \
  -q -rx -p no:cacheprovider \
  experiments/whisperlivekit/test_review.py \
  artifacts/whisperlivekit-review-20260915/WhisperLiveKit-363e4f6d029694d9c81ae548beddd9d3c88a3637/tests/test_retention.py \
  --junitxml=artifacts/whisperlivekit-review-20260915/review-results.xml
```

结果：**14 passed, 3 xfailed**，总计 17 个案例。三项明确的预期失败是：

1. LocalAgreement 将新的、快速重复的 `very` 当成已提交重叠词删除。
2. MLX 翻译分句在 `Dr.` 后提前触发。
3. MLX 翻译分句在 ASR token `3.14` 后提前触发。

使用严格 xfail：如果将来更换上游或修复后行为通过，pytest 会报告 XPASS 并返回失败，提醒更新已知限制记录。本轮 xfail 的实际失败栈保存在 JUnit XML；其余用例也包含“close 会清空音频”“EOF 可留未翻译句”等限制验证，不能把通过数量解读成生产就绪程度。

## 重新准备隔离环境

已有快照目录时只需创建 Python 3.12 虚拟环境并安装 [requirements-review.txt](requirements-review.txt)。例如在项目根目录执行：

```sh
UV_CACHE_DIR=/private/tmp/wlk-uv-cache uv venv --python 3.12 \
  artifacts/whisperlivekit-review-20260915/venv
UV_CACHE_DIR=/private/tmp/wlk-uv-cache uv pip install \
  --python artifacts/whisperlivekit-review-20260915/venv/bin/python \
  -r experiments/whisperlivekit/requirements-review.txt
```

本轮实际使用本机已安装的 `/Users/kevinzhu/.local/share/uv/python/cpython-3.12-macos-aarch64-none/bin/python3.12`。其他机器可用自己安装的兼容 Python；无须更改系统默认 Python。

如果 artifacts 已被清理，先从 [固定源码压缩包](https://codeload.github.com/QuentinFuxa/WhisperLiveKit/tar.gz/363e4f6d029694d9c81ae548beddd9d3c88a3637) 重新取得快照，校验上述 SHA-256，再解压到 `artifacts/whisperlivekit-review-20260915/`。解压后的目录名应为 `WhisperLiveKit-363e4f6d029694d9c81ae548beddd9d3c88a3637`。也可用 `WLK_SOURCE_DIR` 指向另一份已核对的同提交快照；上游测试路径需相应修改。

本实验不使用完整 `pip install whisperlivekit`，不触发 MLX、PyTorch、分词器或权重的下载/加载；没有声称已跑通模型服务。源码压缩包也不含 Git 子模块，当前这些测试不需要子模块。

## 最初无模型测试的验证范围

覆盖：稳定前缀、重叠去重与真实重复、缓冲尾词、队列压力与中断、整句翻译输入、失败重试、停顿/EOF、diff 修订与历史裁剪。

未覆盖：真实识别率、翻译准确率、模型吞吐与字幕延迟、麦克风、WebSocket 服务整体运行、长课堂、离线加载和 Swift 应用构建。这些结果用于决定接入方式及其验收重点。

## 后续 GPU 算子探测

用户进一步询问能否把 PyTorch 解码放到 Mac GPU 上。本轮另建 `artifacts/whisperlivekit-gpu-review-20260915/venv`，保持前述无模型测试环境独立，安装 [requirements-gpu-probe.txt](requirements-gpu-probe.txt)。

[probe_mps.py](probe_mps.py) 直接实例化上游的两层、64 维随机权重 decoder，保留原始稀疏缓冲结构，并测试 MPS 的 FP32、FP16、KV 缓存续写、交叉注意力及中值滤波。无训练模型、真实音频或整个 WLK 服务。

```sh
PYTHONDONTWRITEBYTECODE=1 \
NUMBA_CACHE_DIR=artifacts/whisperlivekit-gpu-review-20260915/numba-cache \
  artifacts/whisperlivekit-gpu-review-20260915/venv/bin/python \
  experiments/whisperlivekit/probe_mps.py \
  --source artifacts/whisperlivekit-review-20260915/WhisperLiveKit-363e4f6d029694d9c81ae548beddd9d3c88a3637 \
  --output artifacts/whisperlivekit-gpu-review-20260915/mps-probe.json
```

脚本在导入 torch 前关闭不支持算子的自动 CPU fallback；MPS 不可用返回退出码 2，算子验证失败返回 1，通过返回 0。本轮需在获准访问宿主 GPU 的进程中执行；沙盒内 MPS 可见性不足不代表硬件不支持。

结果见 [mps-probe-result.json](mps-probe-result.json)：原始模型迁移、MPS 稀疏对齐头索引和所有探测算子均通过。没有测 CPU/GPU 加速比，也未运行真实 checkpoint。解释和接入方向见 [GPU 加速补充](../../docs/WhisperLiveKit-Mac-GPU加速补充.md)。

## 真实模型的 MLX → MPS → SimulStreaming 实验

这一阶段已实际运行 large-v3-turbo 与 `AudioProcessor` 的 PCM 输入、SimulStreaming、结果流和 EOF 管线。运行方式为进程内调用，不启动 WebSocket，不含翻译。五轮结果及其原始记录哈希在 [mps-trial-results.json](mps-trial-results.json)；结论是 GPU 路径可用，短样本尚未显示相对 CPU decoder 的提速。

### 1. 准备补丁源码

从项目根目录执行。首次使用先按本文上方方式准备并校验固定上游快照；目标目录必须不存在，以避免覆盖已有实验。

```sh
python3 experiments/whisperlivekit/prepare_mps_trial.py \
  --source artifacts/whisperlivekit-review-20260915/WhisperLiveKit-363e4f6d029694d9c81ae548beddd9d3c88a3637 \
  --destination artifacts/whisperlivekit-mps-20260915/source
```

脚本校验 [review-metadata.json](review-metadata.json) 的关键源码指纹，并在目标目录的父目录生成 `mps.patch` 和 `patched-source.json`。当前工作区已经准备好这份补丁源码；可直接运行第 4 步。

### 2. 准备 Python 环境

本机扩展了此前的 GPU 探测环境，实际版本已保存到 [requirements-mps-trial.txt](requirements-mps-trial.txt)。若环境不存在：

```sh
UV_CACHE_DIR=/private/tmp/wlk-uv-cache uv venv --python 3.12 \
  artifacts/whisperlivekit-gpu-review-20260915/venv
UV_CACHE_DIR=/private/tmp/wlk-uv-cache uv pip install \
  --python artifacts/whisperlivekit-gpu-review-20260915/venv/bin/python \
  -r experiments/whisperlivekit/requirements-mps-trial.txt
```

这是实际用到的独立依赖环境，不执行完整 WLK extras 安装。本机当时未能解析 `torchaudio==2.14.0`，最终未安装它；本次选定的 ASR 管线无需该包即可运行。

### 3. 准备模型与录音

本机已经下载好 `models/whisper-mps-experiment/large-v3-turbo/`。新环境可以用已安装的 Hugging Face CLI 取得固定版本：

```sh
artifacts/whisperlivekit-gpu-review-20260915/venv/bin/hf download \
  mlx-community/whisper-large-v3-turbo config.json weights.safetensors \
  --revision a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb \
  --local-dir models/whisper-mps-experiment/large-v3-turbo
```

下载阶段需要网络；实际本机下载使用 Python HTTP 流式写入。文件大小和 SHA-256 以 [mps-model-manifest.json](mps-model-manifest.json) 为准，可在运行前校验：

```sh
python3 - <<'PY'
import hashlib, json
from pathlib import Path
manifest = json.loads(Path('experiments/whisperlivekit/mps-model-manifest.json').read_text())
root = Path('models/whisper-mps-experiment/large-v3-turbo')
for name, expected in manifest['files'].items():
    path = root / name
    assert path.stat().st_size == expected['size'], name
    with path.open('rb') as stream:
        assert hashlib.file_digest(stream, 'sha256').hexdigest() == expected['sha256'], name
print('Model hashes verified')
PY
```

上述哈希命令需要 Python 3.11+。编码器与 PyTorch decoder 复用同一份 safetensors，使用 large-v3-turbo 的标准 alignment heads。原生 WhisperKit 的 Core ML 文件不适用于这个加载器。

默认复现录音是当前工作区已有的 `artifacts/smoke-20260914-131929-22067/synthetic-english.aiff`，SHA-256 为 `5a67b0d6251cbb80f69ad09d98a3833ee7f89a4d37cbb906660e2091c2fa7ac7`，参考文本为 `fixtures/translation-en.txt`。若原始 artifacts 被清理，可以用 macOS Samantha 重新合成该文本或指定自己的录音；新录音与已保存结果的字节和耗时不一定一致。

### 4. 运行 MPS FP32

以下命令使用新的输出名，保留原始实测记录。每次重跑自行更换输出名。

```sh
PYTHONDONTWRITEBYTECODE=1 \
NUMBA_CACHE_DIR=artifacts/whisperlivekit-mps-20260915/numba-cache \
  artifacts/whisperlivekit-gpu-review-20260915/venv/bin/python \
  experiments/whisperlivekit/run_mps_trial.py \
  --source artifacts/whisperlivekit-mps-20260915/source \
  --model models/whisper-mps-experiment/large-v3-turbo \
  --audio artifacts/smoke-20260914-131929-22067/synthetic-english.aiff \
  --output artifacts/whisperlivekit-mps-20260915/mps-fp32-local-rerun.json \
  --device mps --dtype float32 --paced \
  > artifacts/whisperlivekit-mps-20260915/mps-fp32-local-rerun.log 2>&1
```

脚本预热后按 0.5 秒 PCM 包的真实时间节奏发送，结束时发 EOF 并收集最终结果。退出码 0 表示设备、接收样本数、错误状态、非空确认文本与暂存尾部检查通过，不代表识别没有错词。输出包括 `.json`、每次前端更新的 `.updates.jsonl` 及日志；首个确认输出是英文前缀，未计算完整句子或中文延迟。

运行器在导入模型库前禁用 MPS 自动 CPU fallback，并设置 Hugging Face / Transformers 离线变量。本机在可访问宿主 GPU 的进程中运行；如果受限进程报告 `mps_available=false`，它会报错，不会悄悄改用 CPU decoder。

对照运行只需修改设备或精度并使用独立输出名：

- MPS FP16：`--device mps --dtype float16`。
- CPU FP32 decoder：`--device cpu --dtype float32`；MLX encoder 仍使用 GPU。
- 所有对照均保留 `--paced`；不加该选项会变为批量送入，不能与表中的实时回放计时直接比较。

### 5. 停顿后重复内容

生成“原录音 + 6 秒静音 + 原录音”夹具：

```sh
artifacts/whisperlivekit-gpu-review-20260915/venv/bin/python - <<'PY'
import numpy as np
import soundfile as sf
audio, rate = sf.read('artifacts/smoke-20260914-131929-22067/synthetic-english.aiff', dtype='float32')
repeated = np.concatenate([audio, np.zeros(rate * 6, dtype=np.float32), audio])
sf.write('artifacts/whisperlivekit-mps-20260915/repeated-after-pause.wav', repeated, rate, subtype='PCM_16')
PY
```

将第 4 步的 `--audio` 改成这个 WAV，并使用新的输出名。已保存的实测是 `mps-fp32-pause-repeat.json`：两次段落开头、退款条件和最终因果句均保留；时间戳仍有约 0.22 秒交叠，不能把该测试当作精确字幕对齐验收。

### 6. 配置检查与结果复核

```sh
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  artifacts/whisperlivekit-gpu-review-20260915/venv/bin/python -m pytest \
  -q -p no:cacheprovider experiments/whisperlivekit/test_mps_trial_config.py
```

本轮结果为 **6 passed**。这些是配置传递与非法值检查；GPU 和实际模型可行性的证据来自五次真实推理。

已有五轮原始记录都在当前工作区时，可不加载模型直接校验并重新生成摘要：

```sh
PYTHONDONTWRITEBYTECODE=1 \
  artifacts/whisperlivekit-gpu-review-20260915/venv/bin/python \
  experiments/whisperlivekit/summarize_mps_trial.py
```

汇总脚本针对已保存的五轮实验，校验补丁文件与模型哈希、输入音频、PCM 样本数、设备/精度、首个确认更新、关键短语次数和 EOF 尾部；保留全部参考文本差异。没有原始 artifacts 时仍可阅读随项目保存的摘要，但不能重新验证那些原始记录。
