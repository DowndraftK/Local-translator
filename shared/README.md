# B02共享文字核心

`translator_core`只依赖CPython标准库：冻结配置与schema1、分段装箱、保存恢复、Ollama请求、调度、有限重试与完整双语TXT都在此处。共享Qt页面与worker传输层位于`prototype/b01`；目录名保留B01历史，当前源码身份为B02预览，不复制第二套业务实现。

Mac适配器`platform/macos/TextBoundaries.swift`仅返回NaturalLanguage句尾和Swift Character的UTF-8边界、空白/换行属性。段落/句子/空白优先级、2048字节预算和纯空白合并仍由共享Python核心处理，用生产`LongTextSplitter`的冻结夹具验证。Windows的本机分句/字符适配尚未实测；不能给不同算法直接套用`natural-language-utf8-v1`身份。已有保存计划恢复时不重新分段。

旧SwiftUI入口及生产Sources/runtime保持可用。B02预览默认使用项目本机`artifacts/b02-20261004/tasks`，根目录须为空或具有预览标记，单写入锁拒绝同时写入。不直接接管旧TextDocuments目录；用户主动选择任务UUID目录后只读导入副本，保存目录和原始快照/source字节保留。损坏current可只读previous并阻止继续，未来schema不降级，封存source缺失/错配不从原路径替换。1MiB输入与32MiB快照上限是当前预览的有限边界。

新任务读取B01生产Swift生成的四份固定recipe中的7B-v2配置；旧v1任务保留原recipe、contextVersion省略、Foundation漂亮/排序/不转义slash编码与binding。Python字幕recipe仍由旧runtime管理，不能互换binding口径。打开/刷新/导出不调用Ollama；开始/主动继续才检查固定0.35.0服务、本机模型digest及本地生成能力，再请求翻译。使用原8192/4096、完整options、keep_alive5m；不自动下载、卸载或控制共享服务。

每段每次用户主动运行只尝试一次、累计最多三次。资源/网络/配置错误立即停止调度，普通不完整结果可继续其他段。成功结果与完成状态在同一durable snapshot落盘；失败立即停调度，内存确认结果可完整导出。取消绑定操作generation，关闭本次拥有的HTTP socket，迟到成功不写入其他操作；正常关闭等待任务尾部保存与worker确认。

验证入口：`prototype/b01/tests/test_text_core.py`、原协议/Qt检查、`Tests/TranslatorCoreTests/B02CompatibilityTests.swift`及`fixtures/b02`。真实Mac/Windows状态以批次开发记录和共同矩阵为准；Qt/CPython依赖继续使用B01固定受控环境，Mac候选仍是开发预览，独立运行包在B09处理。
