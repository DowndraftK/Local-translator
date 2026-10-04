# B02本地worker协议1.1

B02接续[B01协议](跨平台-B01本地worker协议.md)的父进程拥有匿名管道、隔离解释器、真实hello/ready、session/request/operation/lease身份、严格递增事件序号、64KiB JSONL帧、30秒启动确认和安全关闭确认。B01冻结候选继续使用1.0；当前共享源码与候选一起升级为1.1，版本不配显式拒绝，不混接旧进程。原无模型probe保留。

| 请求 | 结果 | 约束 |
| --- | --- | --- |
| task.create | snapshot + task.done | UI暂存本次UTF-8输入，传32位随机hex文件名、byte_count、sha256；worker只读取独立根incoming并核对，最大1MiB |
| task.list / task.open | snapshot + task.done | 只读，打开处理中任务仅在内存标记stopped；不分段、不推理、不自动保存 |
| task.import | snapshot + task.done | 用户显式选择UUID目录，只读复制current/previous/source；重复UUID拒绝覆盖 |
| task.save | snapshot + task.done | 单调revision、durable保存；损坏或源错配只读任务不能继续写 |
| task.start | operation.started / snapshot / operation.finished | 开始或主动继续；复用原段计划，跳过全部成功段，固定recipe/digest/预算 |
| service.check | operation.started / operation.finished | 只确认服务版本、digest和本机生成能力，无模型生成 |
| task.export | task.exported | worker从一致内存状态生成完整双语TXT，返回受控exports随机文件名、byte_count、sha256；UI核对后保存到用户选择位置 |
| lease.renew | lease.renewed | 与当前operation/lease匹配才续期，取消后不续期 |
| cancel / shutdown | 接受 / operation.finished / shutdown.confirmed | 本请求socket取消、已确认结果保存、尾部排空；不停止Ollama或Codex远程服务 |

快照采用snapshot.begin/chunk/end：begin含transfer UUID、UTF-8总字节和sha256；chunk含从0连续index与最多16384原始字节的base64；end后核对总长度/hash再交付UI。传输总上限32MiB。worker串行发送完整快照，允许取消/诊断事件穿插，不允许两个快照交错。活动翻译快照同时绑定operation/lease。旧session、重复/乱序事件、丢失/乱序/过量块、checksum不配均拒绝，不能将半份快照当作结果。

文字操作使用20秒租约，Qt每5秒续期；worker独立监听期限，过期取消拥有socket并保存后报告lease_expired。已完成请求从客户端map清理，避免长操作续期或多次刷新累积到512项上限。取消在headers/readline等待时也可中断；HTTP连接5秒、请求读取120秒、翻译总时限600秒，诊断请求另有10秒上限。

task.done或operation.finished在最后一致快照之后发送。快照携带durable_revision、warning、save_blocked，UI明确区分已落盘状态与保存失败的内存确认结果。业务错误显示具体原因并保留worker；协议错误按B01规则拒绝失配进程。正常OS退出仍由外部观察确认，不能用窗口closeEvent或shutdown.confirmed替代Qt/CPython最终退出证据。

本协议不包含PDF、OCR、SQLite语音任务或安装器入口。Windows原生构建、匿名管道、文件锁/原子保存/路径与GUI尚待后续实机接续；Mac通过不等同Windows通过。
