# 本机流式字幕运行环境

更新：2026-09-17。原生应用已接入此目录，使用本机 Python 工作进程；无需启动 WhisperLiveKit Web 服务。

## 在应用中使用

1. 打开新版应用，在“本机资源”检查 Ollama；未运行时点击“启动本地服务”。默认中文模型为 `hy-mt2:1.8b-q8`。
2. 进入“录音翻译”，导入英语音频/视频，点击“开始处理文件”；或点击“开始麦克风录音”并允许系统权限。模型准备好后才开始采集，准备期间说的话不会录入。
3. 确认的英文先保存，中文独立生成。“按原速回放输入”用于模拟实时输入，并不会自动播放声音。取消此选项可以批量处理文件。
4. 点击“停止并保存”后等待尾句结束。若翻译失败，打开已保存任务并点击“继续补译 / 重试失败”。只补未完成译文，不重新翻译成功段落。
5. 点击片段左侧时间可回放；修改英文后旧译文失效，点击继续补译。修改前的英文保留在数据库中，当前界面还没有完整版本历史浏览器。
6. 完成后可选择“录后重新校对”：用整段保存的音频再次识别，并在**独立的新任务**中生成中文。点击“查看原版”可以返回原字幕；原目录和文字不被覆盖。校对可补救流式漏词，但仍须核对原音频。
7. 通过“导出”保存双语 TXT、SRT 或 WebVTT。时间戳来自模型估计；导出会顺延交叠字幕，以保持播放器可接受的时间顺序。

录音任务默认保存在 `~/Library/Application Support/LocalTranslator/Recordings/<UUID>/`。界面的“打开任务目录”可直接定位。备份时复制整个目录，包含数据库、录音和运行配置；任务运行中不宜只复制数据库主文件，因为未合并事务可能位于 WAL 文件中。

## 当前机器所需资源

应用包内包含 Python 运行代码。以下较大资源留在项目目录，应用中的语音资源位置应选择本项目的 `models` 文件夹：

| 资源 | 项目内位置 |
| --- | --- |
| Python 3.12 环境 | `artifacts/whisperlivekit-gpu-review-20260915/venv` |
| 固定 WhisperLiveKit 补丁源码 | `artifacts/whisperlivekit-mps-20260915/source` |
| 补丁指纹 | `artifacts/whisperlivekit-mps-20260915/patched-source.json` |
| MLX large-v3-turbo 权重 | `models/whisper-mps-experiment/large-v3-turbo` |
| 模型基线 | `experiments/whisperlivekit/mps-model-manifest.json` |
| 中文模型 | 本机 Ollama 中的 `hy-mt2:1.8b-q8` |

资源准备方法见 [固定源码、依赖与模型](../experiments/whisperlivekit/README.md)。当前机器已经准备好。请勿清理上述 `artifacts` 目录后继续使用应用；本版不是脱离项目目录的独立安装包。重建环境后可使用固定的 `requirements-mps-trial.txt`，不会在识别过程中自动下载模型。

实时路线固定为 **MLX GPU 编码 → PyTorch/MPS FP32 解码 → SimulStreaming 提交**，可选 CPU 解码作对照。MPS 不可用时明确失败，禁止静默回退。录后校对采用完整 MLX Whisper 推理，使用相同权重。两者是不同处理方式，校对耗时不能当作实时字幕延迟。

## 开发与复现

以下命令从项目根目录运行；每次新识别或校对使用新目录，已有任务只能补译、修改或导出。

```sh
export PYTHONPATH="$PWD/runtime"
export PYTHONDONTWRITEBYTECODE=1
stream_python="$PWD/artifacts/whisperlivekit-gpu-review-20260915/venv/bin/python"
"$stream_python" -m streaming_translator configure --project . --output artifacts/runtime.json
"$stream_python" -m streaming_translator run \
  --config artifacts/runtime.json --session artifacts/my-recording \
  --input /绝对路径/英语录音.m4a --paced
"$stream_python" -m streaming_translator retry --session artifacts/my-recording --retry-failed
"$stream_python" -m streaming_translator refine \
  --config artifacts/my-recording/runtime.json \
  --from-session artifacts/my-recording --session artifacts/my-recording-refined
"$stream_python" -m streaming_translator export \
  --session artifacts/my-recording-refined --format srt --output artifacts/双语字幕.srt
```

可选实验参数：`--device cpu`、`--dtype float16`、`--coalesce-seconds`、`--max-context-tokens`、`--no-translation`。默认 MPS FP32、0.5 秒合并、128 个历史上下文 token；此设置在单段完整 TED 中降低积压和漏词，仍待多材料验证。`--stdin-pcm` 接收 16 kHz、单声道、16 位有符号小端 PCM，适用于原生采集管线；EOF 提交尾包和尾句。

自动检查：

```sh
PYTHONPATH=runtime PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  artifacts/whisperlivekit-gpu-review-20260915/venv/bin/python -m pytest \
  -q -p no:cacheprovider Tests/Python
bash scripts/swift.sh test
```

## 数据与恢复约定

- `session.sqlite` 是权威记录；事务同时保存确认英文和翻译任务。`snapshot.json` 是供界面读取的可重建投影。
- `audio.wav` 保存归一化音频；文件导入先完整转换，麦克风输入每包先写盘，再交给 ASR。`worker-*.log` / `progress.jsonl` 记录状态与进度。
- 任务锁阻止同时修改同一任务。翻译租约和源文字版本校验阻止旧请求覆盖新修订。
- 重试只恢复翻译队列；**尚不支持从任意识别检查点无缝续录**。中断后可补译已保存英文；未识别部分需重处理原录音，或对已保存音频创建录后校对版。
- 录后校对复制音频，避免原目录清理后丢失回放来源。停止校对会等待当前模型调用退出，可能需几十秒；不会把未提交完的识别标成完成。
- 录后整段解码目前限两小时，内存随音频长度增加；这只是实现上限，尚未通过两小时验收。录中字幕仍按有界音频缓冲处理。

## 已知范围

已验证真人 TED 文件、翻译中断恢复、采样转换与管道输入；真实麦克风的系统权限、设备断开、睡眠恢复及 60–90 分钟课堂尚待实际验收。没有系统声音采集、说话人识别或自动保存所有译文历史。应用仍会出现英文漏词、中文条件误译；“本次处理完成”只表示流程结束。

完整实测、材料来源与限制见 [流式字幕与 TED 实测](../docs/流式字幕与TED实测报告.md)。
