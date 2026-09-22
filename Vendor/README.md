# M0 固定依赖

为保证验证程序构建可复现且不需要构建时继续访问网络，保存所需库的源码快照。没有包含语音或文字模型权重。

| 库 | 固定来源 | 保留范围 |
| --- | --- | --- |
| Argmax OSS Swift / WhisperKit | `argmaxinc/argmax-oss-swift`，提交 `ea872ffd35705aa757f33033500b9b0d40bd38df` | `Sources/ArgmaxCore`、`Sources/WhisperKit`、LICENSE、NOTICES |
| ZIPFoundation | `weichsel/ZIPFoundation`，tag `0.9.20`，查询到的 ref `86fc841708997c5bce8545e23845125f4eb28493` | Sources、LICENSE |

原始包清单保存在各目录的 `Package.upstream.swift`。当前清单只暴露本项目用到的 macOS 库目标；不引入 Argmax CLI、TTS、说话人识别、服务器或它们的额外依赖。保持上游 Swift 5 语言模式兼容；使用本机 Swift 6.3.3 工具链编译。这还不是严格 Swift 6 并发模式的验收。

唯一的推理库行为补丁位于 `argmax-oss-swift/Sources/WhisperKit/Utilities/ModelUtilities.swift`，标记为 `LOCAL_TRANSLATOR_OFFLINE_TOKENIZER`：`loadTokenizer` 只从显式传入的分词器目录加载；缺失或解析失败直接抛错，删除隐式缓存搜索和 Hub 下载后备。保留 WhisperKit 原有模型变体检测及其余推理逻辑。

升级依赖时必须重新审查这条离线路径，运行缺文件/坏文件检查，并用真实语音模型执行加载和断网推理。已通过的本地加载失败测试不等于整个系统网络行为已完成审计。

来源：

- https://github.com/argmaxinc/argmax-oss-swift/tree/ea872ffd35705aa757f33033500b9b0d40bd38df
- https://github.com/weichsel/ZIPFoundation/tree/0.9.20

保留各项目 MIT 许可及第三方声明。未包含上游示例或测试使用的媒体资源。
