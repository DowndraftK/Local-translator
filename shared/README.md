# B02共享文字核心

`translator_core`只依赖CPython标准库：冻结配置与schema1、分段装箱、保存恢复、Ollama请求、调度、有限重试与完整双语TXT都在此处。共享Qt页面与worker传输层位于`prototype/b01`；目录名保留B01历史，当前源码身份为B02预览，不复制第二套业务实现。

Mac适配器`platform/macos/TextBoundaries.swift`仅返回NaturalLanguage句尾和Swift Character的UTF-8边界、空白/换行属性，身份仍为`natural-language-utf8-v1`。Windows使用固定Qt6.10.2的QTextBoundaryFinder提供句子/字素事实，将UTF-16偏移显式映射UTF-8，身份为`qt6102-unicode-utf8-v1`。段落/句子/空白优先级、2048字节装箱预算和调度仍共用；两端复杂字素/原文完整检查已执行。已有保存计划恢复不重新分段或改recipe；没有计划且保存适配身份与当前平台不配的旧草稿拒绝重新规划，保留读取/导出，可在原平台继续或明确另建任务。

旧SwiftUI入口及生产Sources/runtime保持可用。B02预览默认使用项目本机`artifacts/b02-20261004/tasks`，根目录须为空或具有预览标记，单写入锁拒绝同时写入。不直接接管旧TextDocuments目录；用户主动选择任务UUID目录后只读导入副本，保存目录和原始快照/source字节保留。损坏current可只读previous并阻止继续，未来schema不降级，封存source缺失/错配不从原路径替换。1MiB输入与32MiB快照上限是当前预览的有限边界。

新任务读取B01生产Swift生成的四份固定recipe中的7B-v2配置；旧v1任务保留原recipe、contextVersion省略、Foundation漂亮/排序/不转义slash编码与binding。Python字幕recipe仍由旧runtime管理，不能互换binding口径。打开/刷新/导出不调用Ollama；开始/主动继续才检查固定0.35.0服务、本机模型digest及本地生成能力，再请求翻译。使用原8192/4096、完整options、keep_alive5m；不自动下载、卸载或控制共享服务。

每段每次用户主动运行只尝试一次、累计最多三次。资源/网络/配置错误立即停止调度，普通不完整结果可继续其他段。成功结果与完成状态在同一durable snapshot落盘；失败立即停调度，内存确认结果可完整导出。取消绑定操作generation，仅关闭本次拥有的HTTP socket。Windows跨线程关闭不能可靠唤醒长timeout的select，因此在SocketIO内部短轮询，保留120秒读取/600秒翻译预算；流读取发生连接中止时先核对取消身份，未取消的连接错误仍明确失败。迟到成功不写入其他操作；正常关闭等待任务尾部保存与worker确认。

Windows路径保护包括大小写snapshot别名、末尾点/空格、设备名、ADS、NTFS junction及祖先链接。单写入锁只初始化一次锁字节，原子替换和revision高水位保持。仓库`.gitattributes`禁止自动换行转换，冻结recipe/source/夹具继续按原字节读取。Windows固定解释器与Qt只在项目受控目录准备，文件/协议/真实worker检查与GUI结果分别登记。

验证入口：`prototype/b01/tests/test_text_core.py`、原协议/Qt检查、`Tests/TranslatorCoreTests/B02CompatibilityTests.swift`及`fixtures/b02`。真实Mac/Windows状态以批次开发记录和共同矩阵为准；Qt/CPython依赖继续使用B01固定受控环境，Mac候选仍是开发预览，独立运行包在B09处理。
