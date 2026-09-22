import Foundation
import TranslatorCore

@main struct Main {
    static let help = """
    本地翻译器 M0 验证程序（尚非完整应用）

    doctor [--endpoint http://127.0.0.1:11434]
    translate --input 文件 --model 模型名 [--direction en-zh|zh-en] [--glossary JSON] [--output JSON]
    extract --input 文件 [--mode auto|text|ocr|layout] [--pages 10] [--output JSON]
    seal-speech --model-folder 目录 --tokenizer-folder 目录 --output JSON
    transcribe --input 音频 --resources JSON [--model Ollama模型名] --output JSON
    replay --input 音频 --resources JSON --model Ollama模型名 --output JSON

    replay 按文件原始时长分窗口输入，用于测量持续吞吐及积压；它不等于已实现麦克风实时字幕。
    所有推理均使用已准备的本地资源；程序不下载模型。输出文件可能含原文、译文和时间轴。
    """
    static func main() async {
        do { try await run() }
        catch { FileHandle.standardError.write(Data("错误：\(error.localizedDescription)\n".utf8)); exit(1) }
    }
    static func run() async throws {
        let args = Array(CommandLine.arguments.dropFirst())
        guard let command = args.first, command != "--help", command != "help" else { print(help); return }
        var options: [String: String] = [:]
        var i = 1
        while i < args.count {
            guard args[i].hasPrefix("--"), i + 1 < args.count, !args[i + 1].hasPrefix("--") else { throw M0Error.invalid("参数应采用 --名称 值。\n" + help) }
            guard options[args[i]] == nil else { throw M0Error.invalid("参数重复：\(args[i])") }
            options[args[i]] = args[i + 1]; i += 2
        }
        let allowed: Set<String> = ["--input", "--output", "--endpoint", "--model", "--direction", "--glossary", "--mode", "--pages", "--model-folder", "--tokenizer-folder", "--resources"]
        guard Set(options.keys).isSubset(of: allowed) else { throw M0Error.invalid("包含未知参数。") }
        func required(_ key: String) throws -> String {
            guard let value = options[key], !value.isEmpty else { throw M0Error.invalid("缺少 \(key)。") }
            return value
        }
        func inputURL() throws -> URL { URL(fileURLWithPath: try required("--input")) }
        func emit<T: Encodable>(_ value: T) throws {
            if let path = options["--output"] {
                let url = URL(fileURLWithPath: path)
                if let source = options["--input"], URL(fileURLWithPath: source).standardizedFileURL == url.standardizedFileURL {
                    throw M0Error.invalid("输出路径不能覆盖输入文件。")
                }
                try JSONOutput.write(value, to: url)
                print("结果已保存：\(url.path)")
            } else { print(String(decoding: try JSONOutput.encode(value), as: UTF8.self)) }
        }
        let endpoint = options["--endpoint"] ?? "http://127.0.0.1:11434"
        switch command {
        case "doctor":
            struct Report: Encodable { var os: String; var endpoint: String; var reachable: Bool; var models: [OllamaModel]; var notes: [String] }
            let engine = try OllamaEngine(endpoint: endpoint)
            var report = Report(os: ProcessInfo.processInfo.operatingSystemVersionString, endpoint: endpoint, reachable: false, models: [],
                notes: ["仅检查本地服务和模型元数据，不证明模型推理成功或整个系统零联网。", "语音资源需显式提供清单；M0 尚未完成。"])
            do { report.models = try await engine.models(); report.reachable = true }
            catch { report.notes.append("本地服务不可用：\(error.localizedDescription)") }
            try emit(report)
        case "translate":
            let source = try String(contentsOf: inputURL(), encoding: .utf8)
            var glossary: [GlossaryTerm] = []
            if let path = options["--glossary"] { glossary = try JSONDecoder().decode([GlossaryTerm].self, from: Data(contentsOf: URL(fileURLWithPath: path))) }
            let engine = try OllamaEngine(endpoint: endpoint)
            let result = try await engine.translate(source, model: required("--model"), direction: options["--direction"] ?? "en-zh", glossary: glossary)
            try emit(result)
        case "extract":
            guard let pages = Int(options["--pages"] ?? "10") else { throw M0Error.invalid("--pages 必须为整数。") }
            try emit(try await DocumentImporter.extract(inputURL(), mode: options["--mode"] ?? "auto", pageLimit: pages))
        case "seal-speech":
            _ = try required("--output")
            try emit(SpeechResources.seal(model: URL(fileURLWithPath: required("--model-folder")), tokenizer: URL(fileURLWithPath: required("--tokenizer-folder"))))
        case "transcribe", "replay":
            let out = URL(fileURLWithPath: try required("--output"))
            let input = try inputURL()
            guard out.standardizedFileURL != input.standardizedFileURL else { throw M0Error.invalid("不能覆盖原音频。") }
            let manifest = try Data(contentsOf: URL(fileURLWithPath: required("--resources")))
            let resources = try JSONDecoder().decode(SpeechResources.self, from: manifest)
            let model = options["--model"]
            if command == "replay", model == nil { throw M0Error.invalid("replay 需要 --model，才能测量完整双语处理延迟。") }
            let engine = try OllamaEngine(endpoint: endpoint)
            if let model { try await engine.verifyLocalModel(model) }
            let speechStarted = Date()
            let speech = try await SpeechEngine(resources: resources)
            let resourceLoadSeconds = Date().timeIntervalSince(speechStarted)
            var checkpoint: [SpeechSegment] = []
            var result = try await speech.process(url: input, ollama: model == nil ? nil : engine, model: model, replay: command == "replay") { segment in
                checkpoint.append(segment)
                try JSONOutput.write(checkpoint, to: out.appendingPathExtension("partial.json"))
                FileHandle.standardError.write(Data("已完成片段 \(segment.index + 1)，原音频到 \(String(format: "%.1f", segment.end)) 秒\n".utf8))
            }
            result.resourceLoadSeconds = resourceLoadSeconds
            result.totalElapsedSeconds = Date().timeIntervalSince(speechStarted)
            try emit(result)
        default: throw M0Error.invalid("未知命令：\(command)\n" + help)
        }
    }
}
