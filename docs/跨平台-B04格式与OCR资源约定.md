# B04 格式与 OCR 资源约定

在 B03 独立记录/指标提交 `a3da9c2` 后开始 B04。旧任务继续原 schema1、原 recipe/binding、source、计划及成功对象；打开不写回。**新 OCR 任务使用显式 schema2 envelope**，仍有 base64/checksum、current/previous 和单调 revision。schema2 payload 有 formatVersion=2，表示 nullable 原始置信度/坐标、原始识别身份、草稿所归属的有效页修订和核对修订。旧生产 Swift/schema1 读者必须拒绝schema2，不能静默丢字段；future schema仍拒绝。旧schema1 OCR如需编辑/重识别/变方向，用户明确另建副本归属，原任务/成功结果保持，不自动迁移。

原始识别的引擎/资源/语言/顺序/文字/置信度/坐标/渲染/识别修订与有效text/page revision、未应用draft/baseRevision、人工reviewRevision分别保存。开始翻译前必须应用或明确放弃草稿；开始后来源冻结。重新识别、改有效原文/方向/模型须显式新任务；新任务不携带旧成功译文。manual clear/input有独立来源，不算OCR成功。高置信度/翻译完成不自动人工核对。

软件路线有限选择：Mac PDFKit/Vision系统原生，沿旧 accurate、方向明确的英中/中英语言优先级、language correction、orientation up 和原观察顺序；实际revision/OS/可用语言启动probe后登记。Windows选择系统 Windows.Media.Ocr 原生离线实现，不新增未知Windows二进制或在线OCR。只有已有简中资源可用，英语缺失明确拒绝，不暗换识别语言。

已实读 Windows11 `10.0.26200.0`，引擎 `Windows.Media.Ocr.dll` 版本 `10.0.26100.9278 (WinBuild.160101.0800)`，876,544字节，SHA-256 `0db8e48e4f27055f7b7e45412761839b492f5b120b6245edadae61f3fe357ea1`。现有 `zh-Hans-CN` 资源 `C:\Windows\OCR\zh-cn\MsOcrRes.orp`，2,375,822字节，SHA-256 `9dfdfe421624cd8873e13e0746ac0b9f8345729e35b2da6991720bece6ad4db4`。它们是Windows许可下的系统组件，只复用、不再分发。WinRT实际最大边10000，本产品仍用3000/9M/scale≤3，不放宽。WinRT没有提供的confidence留null；word坐标为渲染图左上角像素，不伪造Vision归一化/置信度等价值。

缺英语 `Language.OCR~~~en-US~0.0.1.0`，须匹配build26200，来源Microsoft语言与可选功能官方离线介质，Windows许可；包尚未取得，大小/校验和未知并单列未验证。目标 `artifacts/b04-20261005/resources/windows-en-us`。用户已明确选择本次登记英语缺口，继续其他B04工作；不下载/安装英语包或其他OCR模型。

页面仍串行、有界、每页释放；可直接停止本批当前页事实进程，取消后不接部分/迟到页、不调下一页。原始与有效累计各1MiB UTF-8，草稿累计1MiB，单页最多4096观察项，每任务最多16384观察项（限制空观察项的元数据开销，保持32MiB快照保护）；事实结果文件最多4MiB（双份原始文本和坐标元数据），不改变JSONL帧限额，32MiB快照/4096翻译段不放宽。渲染/PDF/PNG仍受控文件交接，图像不入JSONL，不缓存整书。

必须用新人工栅格化的清晰英/简中PDF验证无文字层和真实识别；旧OCR读取或模拟不替代新识别。引擎正常结束、识别成功、内容准确完整三项分别记录。复杂布局、噪声扫描、手写/纠偏、自动混合与B05—B12不在范围。
