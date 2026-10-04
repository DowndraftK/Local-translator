# B01 本地worker协议 1.0

状态：B01已实施的无模型协议。源码在`prototype/b01/translator_preview`，不接生产任务、模型、GPU或Ollama。本协议用于后续共享核心的操作通道，B01不迁移B02保存恢复业务。旧Swift/Python启动保护、语音有界音频队列和安全提交规则保留。

## 传输、身份与容量

父进程通过QProcess启动明确的本批解释器，`-I -B`和显式bootstrap路径，仅增加原型目录；清理继承Python/Conda/DYLD/Qt插件路径变量，工作目录为原型副本。匿名stdin/stdout管道，无监听端口；stdout只输出UTF-8 JSONL，stderr为错误尾部（UI最多留4096字节）。单帧含换行最多65,536字节，协议major/minor须整数且精确为1/0，非有限数字、错版本、错身份或过大帧拒绝。输入错格式worker退出2；业务错误结构化输出，不默默降级。

每个请求有`protocol`、UUID小写标准形式的`session_id`、`request_id`和`type`。session由父进程每次启动新建并作为启动参数传入；重开/重连改变session。响应回显session/request，增加从1严格递增的`sequence`。同一会话保留最近1024个request ID作去重；父客户端每会话最多512个请求，之后明确失败并需重连，有界性不等于跨进程持久去重。

操作增加`operation_id`和`lease_id`。租约使用本机monotonic相对期限：`lease_ms`100—30,000，任务结束不能复用迟到结果。当前仅允许一个活动操作；`probe.start`固定1—40步骤、每步20—250ms。最大产出有界，不传音频、不新增音频背压规则。生产语音未来仍保持每块0.5秒、最大推理合并1秒、30秒音频队列；不能把B01的小步数容量代替实际语音验证。

## 请求和响应

| 请求 | 实际响应 / 含义 |
| --- | --- |
| `hello` | 进入真实worker处理循环后输出`ready`，带平台/Python/PID/隔离参数和能力；QProcess.started不算就绪 |
| `diagnose` | `diagnostics`，不加载模型/框架，不打开生产任务 |
| `probe.start` | `operation.started`→有限`progress`→`operation.finished`，所有事件绑定请求/操作/租约 |
| `cancel` | 身份匹配才`cancel.accepted`并停止新步骤，排空后输出`operation.finished`；已完成同一操作的竞态返回`cancel.already_finished` |
| `shutdown` | 先取消/等待当前操作尾部，再`shutdown.confirmed`并exit0 |
| stdin EOF | 取消并排空本进程活动probe，退出，不接管外部服务 |
| 未知业务操作 | `error`，code和retryable；文字/文档/语音没有借probe伪实现 |

终态为`completed`、`cancelled`或`lease_expired`，`tail_drained=true`必须先于父客户端认可完成。result为`synthetic-lifecycle-probe`，保留全部已确认步骤、该清单SHA-256、`persistent_task=false`、`inference=false`。没有真实保存任务，因此尾部验证只证明本批有限probe的进程协议，不能充当音频落盘/数据库安全保存证据。

## 父进程状态与停止

`disconnected → connecting → ready → running → cancelling → ready`，停止进入`closing → disconnected`，错误进入`failed`。启动**30秒**未收到匹配ready则明确失败，只停止父进程持有的本批worker；系统进程启动事件/SSH/Codex服务状态不能代替ready。正常关闭先等待协议排空，5秒无退出才强制结束**这一QProcess**，强制路径登记`shutdown_unconfirmed_tail`，不能标安全通过。旧Ollama、旧App、Codex远程服务与其他worker均不按进程名清理。

客户端将exit0但缺ready或shutdown确认视为失败，不以系统退出码替代协议确认。窗口回调只证实窗口/worker排空，OS实际退出须外部等待核对；正常关闭事件不能证明其后的Qt/Python清理未崩溃。

客户端拒绝旧session、乱序/重复sequence、未知request、错操作/lease以及无尾部确认的完成；同窗口重连通过QProcess对象身份隔离旧回调。取消晚于已完成操作只作幂等确认，不把成功结果覆盖成取消。

## 数据兼容边界

协议版本与保存schema/recipe版本分开：TextDocuments schema1、SQLite schema3不升级。Swift binding继续SHA-256绑定Foundation排序/漂亮JSON/不转义slash的实际字节；Swift legacy v1省略contextVersion。Python字幕binding继续绑定紧凑排序UTF-8 JSON；字段名及浮点表示也不同。两种hash不能直接互换，未来共享核心须保留原recipe字节及dialect身份，用已保存binding核对旧成功结果。`fixtures/b01`通过生产编码器生成两方向v1/v2、schema1 current/previous/source和字幕recipe证据，B01只读，续译/写回/SQLite互读留B02。

固定7B-v2完整提示/digest、temperature0.1/top_p0.6/top_k20/repeat_penalty1.0/seed42、8192/4096、keep_alive5m、条件think:false及语音128token保持；Mac MLX编码/MPS FP32解码与封存source未改。Windows跨GPU检查点兼容没有证据，拒绝默认续跑的既定规则保持。
