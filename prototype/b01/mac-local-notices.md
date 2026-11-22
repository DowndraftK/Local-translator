# Mac 本地交付资源与来源

App 随包提供 Python、Qt/PySide6、离线词典和项目的接口代码。Qt/PySide/shiboken 固定在 6.10.2，官方同版本源码和许可由随包 `qt-source-notices` 目录记录。对应 wheel 的补丁、构建和替换条件仍待逐产物核对。Python 许可位于 `python` 目录。

CC-CEDICT 派生词库沿用 CC BY-SA 4.0，并保留上游署名和索引转换说明。ECDICT 仓库的 MIT 许可和 CSV 上游数据再分发材料单独登记。CSV 的完整再分发依据待补齐。词典材料位于 `dictionary` 目录。

翻译连接用户安装的 Ollama。Mac App 随包提供 Whisper 运行组件、分词与 VAD，以及 MLX 转换版 Whisper small.en 英语权重。运行组件的依赖、版本和许可见 `Runtime/whisper/runtime/Licenses`。small.en 转换声明和 OpenAI 上游许可见 `Runtime/whisper/Model-Licenses`。语音运行组件供本机个人使用，部分依赖的对应源码与重链接材料待核对。

当前 App 使用 ad hoc 签名，面向本机使用。Developer ID 签名、公证及公开再分发条件仍待完成。
