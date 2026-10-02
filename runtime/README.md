# 本机流式字幕运行环境

更新：2026-10-01，0.2.6/build21。原生应用已接入此目录，使用本机 Python 工作进程；无需启动 WhisperLiveKit Web 服务。

## 在应用中使用

1. 打开新版应用，在“本机资源”检查 Ollama；未运行时点击“启动本地服务”。默认中文模型为 `hy-mt2:1.8b-q8`。
2. 进入“录音翻译”，导入英语音频/视频，点击“开始处理文件”；或点击“开始麦克风录音”并允许系统权限。模型准备好后才开始采集，准备期间说的话不会录入。
3. 确认的英文先保存，中文独立生成。“按原速回放输入”用于模拟实时输入，并不会自动播放声音。取消此选项可以批量处理文件。
4. 点击“停止并保存”后等待尾句结束。若翻译失败，打开已保存任务并点击“继续补译 / 重试失败”。只补未完成译文，不重新翻译成功段落。
5. 麦克风可以暂停、继续，设备变化或休眠后需主动继续；勾选“仅录音，稍后识别”可以先保存音频。已保存任务识别中断时可点击“继续识别”。
6. 点击片段左侧时间可回放；修改英文后旧译文失效，点击继续补译。修改前的英文保留在数据库中，当前界面还没有完整版本历史浏览器。
7. 完成后可选择“录后重新校对”：用整段保存的音频再次识别，并在**独立的新任务**中生成中文。点击“查看原版”可以返回原字幕；原目录和文字不被覆盖。校对可补救流式漏词，但仍须核对原音频。
8. 通过“导出”保存双语 TXT、SRT 或 WebVTT。页面回放与三种导出共用有效媒体时间；保留交叠，不再逐段顺延。异常时间会提示核对，处理结束不代表内容或声音定位准确。

录音任务默认保存在 `~/Library/Application Support/LocalTranslator/Recordings/<UUID>/`。界面的“打开任务目录”可直接定位。备份时复制整个目录，包含数据库、录音和运行配置；任务运行中不宜只复制数据库主文件，因为未合并事务可能位于 WAL 文件中。

## 当前机器所需资源

应用包内包含 Python 运行代码。以下较大资源留在项目目录，应用中的语音资源位置应选择本项目的 `models` 文件夹：

| 资源 | 项目内位置 |
| --- | --- |
| Python 3.12 环境 | `artifacts/whisperlivekit-gpu-review-20260915/venv` |
| 固定 WhisperLiveKit 补丁源码 | `artifacts/whisperlivekit-speech-repair-20261001-final/source` |
| 补丁指纹 | `artifacts/whisperlivekit-speech-repair-20261001-final/patched-source.json` |
| MLX large-v3-turbo 权重 | `models/whisper-mps-experiment/large-v3-turbo` |
| 模型基线 | `experiments/whisperlivekit/mps-model-manifest.json` |
| 中文模型 | 本机 Ollama 中的 `hy-mt2:1.8b-q8` |

资源准备方法见 [固定源码、依赖与模型](../experiments/whisperlivekit/README.md)。当前机器已经准备好。请勿清理上述 `artifacts` 目录后继续使用应用；本版不是脱离项目目录的独立安装包。重建环境后可使用固定的 `requirements-mps-trial.txt`，不会在识别过程中自动下载模型。

实时路线固定为 **MLX GPU 编码 → PyTorch/MPS FP32 解码 → SimulStreaming 提交**，可选 CPU 解码作对照。MPS 不可用时明确失败，禁止静默回退。录后校对采用完整 MLX Whisper 推理，使用相同权重。两者是不同处理方式，校对耗时不能当作实时字幕延迟。

## 开发与复现

以下命令从项目根目录运行；每次新识别或校对使用新目录，已有任务支持继续识别、补译、修改或导出。

```sh
export PYTHONPATH="$PWD/runtime"
export PYTHONDONTWRITEBYTECODE=1
stream_python="$PWD/artifacts/whisperlivekit-gpu-review-20260915/venv/bin/python"
"$stream_python" -m streaming_translator configure --project . --output artifacts/runtime.json
"$stream_python" -m streaming_translator run \
  --config artifacts/runtime.json --session artifacts/my-recording \
  --input /绝对路径/英语录音.m4a --paced
"$stream_python" -m streaming_translator retry --session artifacts/my-recording --retry-failed
# 只对识别尚未完成的任务运行；先核对录音与资源，再从安全检查点重做尾部
"$stream_python" -m streaming_translator resume --session artifacts/my-recording
"$stream_python" -m streaming_translator refine \
  --config artifacts/my-recording/runtime.json \
  --from-session artifacts/my-recording --session artifacts/my-recording-refined
"$stream_python" -m streaming_translator export \
  --session artifacts/my-recording-refined --format srt --output artifacts/双语字幕.srt
```

可选实验参数：`--device cpu`、`--dtype float16`、`--coalesce-seconds`、`--max-context-tokens`、`--no-translation`。默认 MPS FP32、0.5秒最小合并间隔、128个历史上下文token；每个入队音频块最多0.5秒，每次推理最多合并1.0秒，队列仍30秒有界。实际声音对齐不改写确认前缀的保留坐标。多材料及自然课堂质量仍待验证。`--stdin-pcm` 接收 16 kHz、单声道、16 位有符号小端 PCM，适用于原生采集管线；EOF 提交尾包和尾句。

自动检查：

```sh
PYTHONPATH=runtime PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  artifacts/whisperlivekit-gpu-review-20260915/venv/bin/python -m pytest \
  -q -p no:cacheprovider Tests/Python
bash scripts/swift.sh test
```

## 数据与恢复约定

- `session.sqlite` 是权威记录；事务同时保存确认英文和翻译任务。`snapshot.json` 是供界面读取的可重建投影。
- `audio.wav` 保存归一化音频；文件导入先完整转换，麦克风由独立接收任务写盘并同步，ASR 读取已经落盘的音频。`worker-*.log` / `progress.jsonl` 记录状态与进度。
- 任务锁阻止同时修改同一任务。翻译租约和源文字版本校验阻止旧请求覆盖新修订。
- “继续补译”只恢复翻译队列。“继续识别”从已排空的静音/暂停检查点恢复已保存音频；先校验资源和音频，再把不确定尾部归档到 `recovery_history` 后重做。没有检查点则从头重做，保留旧内容恢复记录；不恢复任意位置的模型内部状态，也不继续已结束任务的麦克风采集。
- `capture-events.jsonl` 记录暂停/继续的样本位置及实际时刻；暂停期间不补假静音。WAV 时间轴只包含实际录音，墙钟缺口保存在事件日志中。
- 数据库版本 3 自动兼容版本 1/2 的原数据库，新增段落时间证据；旧任务缺失证据标为未知，拒绝更新的未知版本。界面每页 200 段、支持查看早期字幕，导出始终包含全部段落。
- 录后校对复制音频，避免原目录清理后丢失回放来源。停止校对会等待当前模型调用退出，可能需几十秒；不会把未提交完的识别标成完成。
- 录后整段解码目前限两小时，内存随音频长度增加；这只是实现上限，尚未通过两小时验收。录中字幕仍按有界音频缓冲处理。

## 0.2.6 修复与重建

本版在已有确认词/窗口/媒体原点修复上，补齐注意力回退保留此前接受词、0.5秒入队/1秒最大推理合并，以及终结尾词和安全的编码窗口右侧补零余量。显示时间复用同模型对齐头、DTW与实际波形静音依据；内部前缀仍按每批确认时的输入终点保留。保留真实重复、原始事件、原始argmax估计和新对齐证据；模型仍可能漏词、错词或产生多余词。

VAD 排空后的真实静音持续至少 0.5 秒时，已确认而无标点的文字以“停顿暂分”保存，避免几句合并导致回放过早；这是保守片段边界，仍需核对上下文。VAD 有前后保留量，这个阈值不是说话人停顿总时长。持续活跃音频无确认词、模型回退等问题会显示最近回放位置。原始估计、有效值和依据保存在 `segment_timing`，不会为了导出顺序累计挪动后续字幕。

新任务使用以下固定目录。旧任务继续识别仍沿用自身 `runtime.json` 和检查点资源指纹，不偷偷切换补丁或复用不匹配的检查点；若需对照新版，请导入已保存 WAV 创建新任务。录后校对始终另建版本。

```sh
python3 experiments/whisperlivekit/prepare_stream_repair.py \
  --source artifacts/whisperlivekit-review-20260915/WhisperLiveKit-363e4f6d029694d9c81ae548beddd9d3c88a3637 \
  --destination artifacts/whisperlivekit-speech-repair-20261001-final/source \
  --variant bounded-acoustic-terminal --max-inference-seconds 1.0 \
  --prefix-retention input-horizon --terminal-padding-room
```

目标目录必须不存在。脚本先校验固定上游与额外变更文件指纹，再生成 MPS/质量补丁和全部 Python 源码清单；不会覆盖旧安装，也不在运行时重新封存已改代码。此补丁针对当前 PyTorch 解码路径，不启用 full-MLX 流式解码。

补丁与窗口逻辑回归：

```sh
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  artifacts/whisperlivekit-gpu-review-20260915/venv/bin/python -m pytest \
  -q -p no:cacheprovider experiments/whisperlivekit/test_speech_quality_patch.py
```

本轮build21的84项Python、42项Swift通过（另1项OCR跳过）；最终短样本、20句波形锚点、模拟暂停/恢复、独立录后版和新包检查有明确证据。57分钟定向长测已完成：五轮WER5.18%→2.05%、删除309→27词，各轮最长连续缺口1词；818段中文和全部样本/三格式完整。本次覆盖的旧循环未复现，不能保证任意模型循环消失。时间锚点达到本轮工程目标，但首句仍约提前1秒，音乐歌词仍严重缺失，实体设备及课堂质量未验收。结果见[追加修复记录](../docs/0.2.6长会话与边界修复记录.md)，build18结论保留于[原记录](../docs/0.2.6语音质量开发记录.md)。

## 已知范围

已验证真人 TED 文件、翻译中断恢复、采样转换、管道落盘及实体麦克风的允许权限、暂停/继续与停止保存。09-23 真实模型异常恢复及 60 分钟重复 TED 原速回放已完成，841 次翻译全部收尾；发现的纯标点字幕已修复，完整事件重放和 45 项 Python 回归通过。长测仍暴露漏词、重复识别与估计时间戳异常，未通过真实课堂质量验收。真实设备断开、拒绝/撤回权限、整机睡眠尚待实际验收。详细结果见 [0.2.2 开发记录](../docs/0.2.2录音与恢复开发记录.md)。没有系统声音采集、说话人识别或自动保存所有译文历史。应用仍会出现英文漏词、中文条件误译；“本次处理完成”只表示流程结束。

完整实测、材料来源与限制见 [流式字幕与 TED 实测](../docs/流式字幕与TED实测报告.md)。

## 0.2.7 文字与文档保存目录

文字长文、PDF/TXT整篇及显式OCR使用独立的 `~/Library/Application Support/LocalTranslator/TextDocuments`，每任务保留schema1原子快照、上一有效版本和源文件副本。打开任务不自动推理，用户主动继续未完成段/页；输入与OCR草稿自动保存但未落盘缓冲可能在异常退出时丢失。格式、保存边界与配置版本交接见[0.2.7开发记录](../docs/0.2.7文字文档保存恢复开发记录.md)。本轮没有迁移或改写Recordings与现有SQLite录音任务数据库。
