@preconcurrency import AVFoundation
import CryptoKit
import Foundation
import WhisperKit

public struct ResourceFile: Codable {
    public var path: String
    public var sha256: String
}

public struct SpeechResources: Codable {
    public var modelFolder: String
    public var tokenizerFolder: String
    public var files: [ResourceFile]

    private static func hash(_ url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var sha = SHA256()
        while let data = try handle.read(upToCount: 1_048_576), !data.isEmpty { sha.update(data: data) }
        return sha.finalize().map { String(format: "%02x", $0) }.joined()
    }
    public static func seal(model: URL, tokenizer: URL) throws -> SpeechResources {
        var files: [ResourceFile] = []
        for folder in Set([model.standardizedFileURL, tokenizer.standardizedFileURL]) {
            guard let enumerator = FileManager.default.enumerator(at: folder, includingPropertiesForKeys: [.isRegularFileKey, .isSymbolicLinkKey], options: [.skipsHiddenFiles]) else {
                throw M0Error.invalid("无法枚举模型目录。")
            }
            for case let url as URL in enumerator {
                let values = try url.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey])
                if values.isSymbolicLink == true { throw M0Error.invalid("资源目录不接受符号链接；请先复制真实文件。") }
                if url.pathExtension == "incomplete" { throw M0Error.invalid("资源目录包含尚未下载完成的文件。") }
                if values.isRegularFile == true { files.append(ResourceFile(path: url.path, sha256: try hash(url))) }
            }
        }
        let resource = SpeechResources(modelFolder: model.path, tokenizerFolder: tokenizer.path, files: files.sorted { $0.path < $1.path })
        try resource.validate()
        return resource
    }
    public func validate() throws {
        guard !files.isEmpty else { throw M0Error.unavailable("语音资源清单为空。请先显式准备模型和分词器。") }
        let modelURL = URL(fileURLWithPath: modelFolder).standardizedFileURL
        let tokenizerURL = URL(fileURLWithPath: tokenizerFolder).standardizedFileURL
        for file in files {
            let url = URL(fileURLWithPath: file.path).standardizedFileURL
            guard [modelURL, tokenizerURL].contains(where: { url.path.hasPrefix($0.path + "/") }) else {
                throw M0Error.invalid("资源文件超出模型或分词器目录。")
            }
            guard FileManager.default.fileExists(atPath: file.path), try Self.hash(url) == file.sha256 else {
                throw M0Error.unavailable("资源缺失或校验失败：\(url.lastPathComponent)。不会自动下载。")
            }
        }
        for name in ["tokenizer.json", "tokenizer_config.json"] {
            let url = tokenizerURL.appendingPathComponent(name)
            guard files.contains(where: { $0.path == url.path }) else { throw M0Error.unavailable("清单缺少 \(name)。") }
            guard (try JSONSerialization.jsonObject(with: Data(contentsOf: url))) is [String: Any] else { throw M0Error.invalid("分词器配置损坏。") }
        }
        for name in ["AudioEncoder", "TextDecoder", "MelSpectrogram"] {
            guard files.contains(where: { $0.path.hasPrefix(modelURL.path + "/" + name + ".mlmodelc/") }) else {
                throw M0Error.unavailable("未找到已编译 Core ML 资源 \(name).mlmodelc。")
            }
        }
    }
}

public struct SpeechSegment: Codable {
    public var index: Int
    public var start: Double
    public var end: Double
    public var english: String
    public var chinese: String?
    public var processingSeconds: Double
    public var lagSeconds: Double?
}

public struct SpeechRun: Codable {
    public var source: String
    public var segments: [SpeechSegment]
    public var elapsedSeconds: Double
    public var audioSeconds: Double
    public var mode: String
    public var warnings: [String]
    public var resourceLoadSeconds: Double? = nil
    public var totalElapsedSeconds: Double? = nil
}

public final class SpeechEngine {
    private let pipe: WhisperKit
    public init(resources: SpeechResources) async throws {
        try resources.validate()
        let config = WhisperKitConfig(modelFolder: resources.modelFolder,
                                      tokenizerFolder: URL(fileURLWithPath: resources.tokenizerFolder),
                                      verbose: false, prewarm: false, load: true, download: false)
        // Build preparation removes the upstream tokenizer network fallback. Never bypass it.
        pipe = try await WhisperKit(config)
    }

    public func process(url: URL, ollama: OllamaEngine?, model: String?, replay: Bool = false,
                        onSegment: ((SpeechSegment) throws -> Void)? = nil) async throws -> SpeechRun {
        let file = try AVAudioFile(forReading: url)
        let inputFormat = file.processingFormat
        guard inputFormat.sampleRate > 0, file.length > 0,
              let outputFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16000, channels: 1, interleaved: false),
              let converter = AVAudioConverter(from: inputFormat, to: outputFormat) else {
            throw M0Error.invalid("无法将音频转换为 16 kHz 单声道。")
        }
        let duration = Double(file.length) / inputFormat.sampleRate
        var result = SpeechRun(source: url.lastPathComponent, segments: [], elapsedSeconds: 0, audioSeconds: duration,
                               mode: replay ? "paced-file-replay" : "file-batch",
                               warnings: ["M0 使用独立音频窗口；尚未完成跨窗口稳定文本合并。窗口边界可能漏词或重复，需人工核对。",
                                          "语音时间戳是模型估计；本结果未经过真实课堂质量验收。"])
        let started = Date()
        let chunkFrames = AVAudioFrameCount(inputFormat.sampleRate * 15)
        var index = 0
        while file.framePosition < file.length {
            try Task.checkCancellation()
            let offset = Double(file.framePosition) / inputFormat.sampleRate
            guard let input = AVAudioPCMBuffer(pcmFormat: inputFormat, frameCapacity: chunkFrames) else { throw M0Error.invalid("音频缓冲创建失败。") }
            try file.read(into: input, frameCount: chunkFrames)
            let end = offset + Double(input.frameLength) / inputFormat.sampleRate
            if replay {
                let remaining = end - Date().timeIntervalSince(started)
                if remaining > 0 { try await Task.sleep(nanoseconds: UInt64(remaining * 1_000_000_000)) }
            }
            let processingStart = Date()
            let capacity = AVAudioFrameCount(ceil(Double(input.frameLength) * 16000 / inputFormat.sampleRate)) + 1024
            guard let output = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: capacity) else { throw M0Error.invalid("音频缓冲创建失败。") }
            converter.reset()
            var provided = false
            var error: NSError?
            let status = converter.convert(to: output, error: &error) { _, state in
                if provided { state.pointee = .endOfStream; return nil }
                provided = true; state.pointee = .haveData; return input
            }
            if let error { throw error }
            guard status != .error, let floats = output.floatChannelData?[0] else { throw M0Error.invalid("音频转换失败。") }
            let samples = Array(UnsafeBufferPointer(start: floats, count: Int(output.frameLength)))
            // Skip near-digital silence; this is not a classroom VAD or a hallucination guarantee.
            let energy = samples.reduce(0.0) { $0 + Double($1 * $1) } / Double(max(samples.count, 1))
            if energy < 0.0000001 { continue }
            let options = DecodingOptions(task: .transcribe, language: "en", temperature: 0,
                                          skipSpecialTokens: true, withoutTimestamps: false)
            let transcription = try await pipe.transcribe(audioArray: samples, decodeOptions: options)
            for part in transcription {
                for segment in part.segments {
                    let text = segment.text.trimmingCharacters(in: .whitespacesAndNewlines)
                    guard !text.isEmpty else { continue }
                    var translated: String?
                    if let ollama, let model { translated = try await ollama.translate(text, model: model).translation }
                    let segmentStart = min(end, max(offset, offset + Double(segment.start)))
                    let segmentEnd = min(end, max(segmentStart, offset + Double(segment.end)))
                    let item = SpeechSegment(index: index, start: segmentStart,
                        end: segmentEnd, english: text, chinese: translated,
                        processingSeconds: Date().timeIntervalSince(processingStart),
                        lagSeconds: replay ? max(0, Date().timeIntervalSince(started) - segmentEnd) : nil)
                    result.segments.append(item); try onSegment?(item); index += 1
                }
            }
        }
        result.elapsedSeconds = Date().timeIntervalSince(started)
        return result
    }
}
