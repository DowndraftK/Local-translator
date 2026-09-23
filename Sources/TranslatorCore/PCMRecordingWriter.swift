@preconcurrency import AVFoundation
import Foundation

/// Call serially. Pauses preserve the PCM sample clock and drain the resampler.
/// The Python receiver acknowledges durability independently after syncing WAV.
public final class PCMRecordingWriter {
    private let output: FileHandle
    private let journal: URL
    private var converter: PCM16StreamConverter?
    private var closed = false
    public private(set) var samples: Int64 = 0

    public init(output: FileHandle, journal: URL) {
        self.output = output; self.journal = journal
    }

    public func begin(format: AVAudioFormat) throws {
        guard !closed, converter == nil else { throw M0Error.invalid("录音尚未暂停或已经关闭。") }
        converter = try PCM16StreamConverter(inputFormat: format)
        try event("resumed")
    }

    public func append(_ buffer: AVAudioPCMBuffer) throws {
        guard let converter, !closed else { throw M0Error.invalid("录音输入已经暂停。") }
        try write(converter.convert(buffer))
    }

    public func finishPart(kind: String) throws {
        if let converter {
            defer { self.converter = nil }
            try write(converter.finish())
        }
        try event(kind)
    }

    public func close() throws {
        guard !closed else { return }
        defer { closed = true; try? output.close() }
        try finishPart(kind: "stopped")
    }

    private func write(_ data: Data) throws {
        if !data.isEmpty { try output.write(contentsOf: data); samples += Int64(data.count/2) }
    }

    private func event(_ kind: String) throws {
        if !FileManager.default.fileExists(atPath: journal.path) {
            guard FileManager.default.createFile(atPath: journal.path, contents: nil) else {
                throw M0Error.invalid("无法保存录音状态。")
            }
        }
        let handle = try FileHandle(forWritingTo: journal)
        defer { try? handle.close() }
        try handle.seekToEnd()
        var data = try JSONSerialization.data(withJSONObject: [
            "id": UUID().uuidString, "kind": kind, "sample": samples,
            "wall_time": Date().timeIntervalSince1970
        ], options: [.sortedKeys])
        data.append(10)
        try handle.write(contentsOf: data)
        try handle.synchronize()
    }
}
