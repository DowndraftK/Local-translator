# B03 生产 Swift 兼容夹具

两个固定 UUID 的 schema1 current/previous 由生产 `TaskRecoveryStore` / `JSONOutput` 生成，分别包含文字层文档和旧 OCR、旧 v1 原 recipe/binding、页内来源映射、成功前缀和未应用旧草稿。`B03CompatibilityTests.swift` 验证生产读取；共享检查在临时副本中续译，不修改这些冻结字节。

`source-bytes.json` 仅含自编三页最小测试 PDF 的 base64 和 hash，供检查临时还原封存来源；不是用户文档或真实任务。实际 B03 文件、导出、截图、完整日志和所有真实任务只留忽略目录。生成证据在 `artifacts/b03-20261005/preflight/swift-fixture-generation-r3.log`，manifest 记录冻结字节。
