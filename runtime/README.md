# 本机流式字幕运行环境

更新：2026-10-03，0.2.9/build35个人自用正式初版已交付。实盘/进程故障、内置麦克风首次拒绝/撤回/重新允许、暂停/继续/停止、真实休眠及麦克风满盘验收与必要修复完成。无外置输入设备，实际拔插未验证且用户已明确接受。0.2.6封存识别源码保持，0.2.8固定7B-v2及旧任务兼容不变；运行层适配器为真实退出故障关闭ONNX遥测，识别算法/配置、翻译recipe及资源校验保持。

## 在应用中使用

1. 打开新版应用，在“本机资源”检查 Ollama；未运行时点击“启动本地服务”。新任务默认中文模型为 `hy-mt2:7b-q8` 和保真配置v2；可手动选择1.8B缩短等待，旧任务继续使用自身保存的模型与配置。
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
| 中文模型 | 本机 Ollama 中的 `hy-mt2:7b-q8`（新默认），`hy-mt2:1.8b-q8`（手动速度选项及旧任务） |

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

## 0.2.8 字幕翻译配置与兼容

`translation_config.py`为新run/record任务保存完整v2recipe：提示 `hy-mt2-faithful-v2`、实际user_prefix、模型及digest、温度0.1、top_p 0.6、top_k 20、repeat_penalty 1.0、seed 42、8192上下文/4096输出、keep_alive 5m、disable_thinking、分段 `subtitle-confirmed-v1` 和上下文 `none-v1`。没有额外跨段上下文。所有GPU调用仍串行，缺少/不匹配资源明确失败，不下载、不换模型或关闭校验。

SQLite仍为schema3，本轮不迁移表结构；recipe保存在metadata的translation_configuration，translation_model_digest与之绑定。请求claim前验证本机权重，成功result_json记录实际request_configuration和configuration_binding；claim/finish同事务约定加上配置binding检查，继续保留原文修订与租约保护。已有完整配置不能被新默认替换，首次仅允许补未知digest。正常结束、非空、未截断及配置/修订/租约全部匹配才提交成功译文。

缺少完整recipe的旧字幕任务依据已知历史v1、保存digest和已成功结果的profile恢复：原温度0.7、repeat_penalty 1.05、无seed、原提示，不套用v2。旧成功row不改写、不补写新配置；不兼容profile或模型digest明确拒绝。缺少的历史证据无法事后证明原权重或参数，只保留已有证据。打开不启动推理，主动“继续补译 / 重试失败”只处理未完成项；顶部模型选择用于新任务，界面另显示当前任务保存模型。录后独立校对任务继承父任务recipe。

单次字幕源最多2048 UTF-8字节，提示加framing不得超过1024字节，与阅读引擎保守预算一致。超过预算时保存英文并明确失败，可修订或另建文字任务，不静默截短原文。新参数不能复用旧任务结果；要采用新方案应另建任务，保留原任务和译文。

本轮30条自编材料的助手语义审阅支持选择7B-v2，但before/by日期边界与median中位数误译仍存在，用户未人工复核，不能宣称中文质量全面通过。五条核对英文短句、2秒到达间隔的串行序列：7B-v2完成中位0.927秒、最大1.173秒，最大排队约5.1毫秒，队列收尾；不能外推课堂端到端或长时吞吐。完整证据与代价见[0.2.8开发记录](../docs/0.2.8翻译质量评审与模型优化开发记录.md)及[紧凑指标](../docs/0.2.8翻译质量指标.json)。

受影响运行层完整Tests/Python实跑72项通过（1.87秒），Swift登记60、执行59通过、按需OCR跳过1。最终build28实际打开自编保存英文的旧v1/新v2测试任务，各由1成功2待补译到3/3，原成功row保持、SRT/TXT导出完整。未重新运行ASR长测、实体麦克风、休眠或实际磁盘不足；封存补丁及历史录音任务不改。

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

已验证真人 TED 文件、翻译中断恢复、采样转换、管道落盘及实体麦克风的允许权限、暂停/继续与停止保存。09-23 真实模型异常恢复及 60 分钟重复 TED 原速回放已完成，841 次翻译全部收尾；发现的纯标点字幕已修复，完整事件重放和 45 项 Python 回归通过。长测仍暴露漏词、重复识别与估计时间戳异常，未通过真实课堂质量验收。0.2.2当轮真实设备断开、拒绝/撤回权限和整机睡眠尚待验收；0.2.9已补内置权限及整机休眠，外置拔插仍未验证且用户接受。详细历史结果见 [0.2.2 开发记录](../docs/0.2.2录音与恢复开发记录.md)。没有系统声音采集、说话人识别或自动保存所有译文历史。应用仍会出现英文漏词、中文条件误译；“本次处理完成”只表示流程结束。

完整实测、材料来源与限制见 [流式字幕与 TED 实测](../docs/流式字幕与TED实测报告.md)。

## 0.2.7 文字与文档保存目录

文字长文、PDF/TXT整篇及显式OCR使用独立的 `~/Library/Application Support/LocalTranslator/TextDocuments`，每任务保留schema1原子快照、上一有效版本和源文件副本。打开任务不自动推理，用户主动继续未完成段/页；输入与OCR草稿自动保存但未落盘缓冲可能在异常退出时丢失。格式、保存边界与配置版本交接见[0.2.7开发记录](../docs/0.2.7文字文档保存恢复开发记录.md)。本轮没有迁移或改写Recordings与现有SQLite录音任务数据库。

## 0.2.9 实盘故障与进程恢复

真实32MiB独立HFS+卷发现后台snapshot/metadata发布失败后采集仍继续且exit0；现在这两个后台任务异常停止输入，并以failed/非零退出报告。满盘可能连日志也无法写入，原生应用提供非空“保存未确认”提示，不能把旧快照当作仍在录音；用户主动停止也不能屏蔽非零失败。隐藏开发参数`--recordings-root`与既有`--recovery-root`用于隔离测试，正常用户保存目录不变。

实盘WAV/状态写满后worker非零停止，已保存16,000样本和SQLite提交前缀保持、integrity_check为ok。独立45秒文件在有安全检查点时SIGKILL，恢复保留2段安全原文、归档1段不确定尾部，8段完成且720,000样本音频不变；另建7B-v2字幕夹具做1成功/1运行时异常退出，补译到8/8，成功row和完整recipe保持，三格式导出。它们不替代真实麦克风权限、拔插或整机睡眠。

完整运行层Tests/Python本轮76项通过，含2个新保存故障回归、1个启动协议实进程回归及1个真实CPU VAD会话/退出回归；Swift登记60/实际59通过/1按需OCR跳过。原生文字/文档快照与源副本也在实盘验收，错误可见、旧保存保持，释放后主动保存成功并清除旧保存错误。详见[0.2.9验收记录](../docs/0.2.9实体故障与恢复验收记录.md)及[使用与恢复说明](../docs/正式初版使用与恢复说明.md)。

可清理小卷的开发复验须先自行建立并挂载独立不超过64MiB测试存储，再显式传入：

```sh
artifacts/whisperlivekit-gpu-review-20260915/venv/bin/python \
  experiments/streaming-translator/fault_storage_trial.py \
  --volume /绝对路径/独立测试卷 \
  --runtime /绝对路径/本地翻译器.app/Contents/Resources/StreamingRuntime \
  --output artifacts/本轮紧凑结果.json
```

脚本只填该卷至实际ENOSPC，释放自己创建的filler，保留证据；不使用麦克风或GPU。不要将系统盘或唯一任务目录作为测试卷。它是实盘运行层故障检查，不能作为麦克风UI或休眠通过证据。

原生字幕导出曾两次停在Python初始化文件读取，尚未进入运行层，日志为空；命令行相同runtime导出正常。非零结束提示可见，现场保留。用户解锁后build33原生SRT导出复验通过，输出与已保存8段字幕字节一致；尚未证实此前阻塞的唯一原因。

build33加入原生启动确认协议：独立环境token只在原生启动时产生确认日志，普通CLI单条JSON不变；30秒尚未确认启动则停止并明确报错，保留已保存任务。导出运行中显示“正在导出字幕”，exit0才显示导出成功。启动协议自动回归与原生成功入口已通过。build35真实未确认启动30秒时保护生效；本次窄范围TCC记录明确Documents授权code requirement不匹配。用户重新允许本应用文稿、正常退出后重开同一build35，原生5段SRT导出成功，与已保存文件逐字节一致。不能把这一根因外推为build32历史阻塞的唯一原因。build34修正权限拒绝后陈旧状态，并在实际撤权后复验。

build34实机正常录音最终608,000样本/38秒；暂停45.06秒不增长，整机睡眠28秒没有假PCM，唤醒需用户主动继续。撤权生效后拒绝开始、不建立任务，旧保存保持；重新允许后新录音成功。独立卷真实麦克风满盘最后有效1,089,140样本/68.07125秒，明确报错并停止、PCM前缀保持；恢复0确认英文段，不能声称该次验证补译。

build34实际38秒录音恢复已提交5/5但外层exit139，系统崩溃栈为ONNX Runtime1.30.0遥测上传线程。build35的asr.py在WLK创建VAD前调用公开disable_telemetry_events()，不覆盖封存源码、不改识别配置/翻译recipe/校验。真实CPU会话退出回归和实际录音恢复外层exit0复验通过，原成功row及音频保留。恢复试验用包内代码，测试驱动仅推迟翻译到ASR完成以保持GPU串行；不是原生默认调度变更。局部遥测处理不等于完整断网审计。

最终build35原生读取38秒5/5任务不推理，回放启动/暂停、SRT导出完成。完整逐项证据与最后保存边界见第三轮记录；原生最多24个已复制缓冲加tap/转换器、管道及运行层块，未记录积压无法给可靠总秒数上界，不承诺未写入缓冲零损失。本轮应用/worker与测试挂载已结束，最新证据及镜像保全在忽略目录，原Ollama保留。
