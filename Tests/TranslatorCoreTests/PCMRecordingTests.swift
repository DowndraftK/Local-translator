import AVFoundation
import Foundation
import Testing
@testable import TranslatorCore

@Test func pausedRecordingDrainsTailAndKeepsSampleClockAcrossDeviceRates() throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: directory) }
    let pcm = directory.appendingPathComponent("audio.pcm")
    let journal = directory.appendingPathComponent("events.jsonl")
    FileManager.default.createFile(atPath: pcm.path, contents: nil)
    let writer = PCMRecordingWriter(output: try FileHandle(forWritingTo: pcm), journal: journal)
    var expected: Int64 = 0
    for rate in [44100.0, 48000.0, 16000.0] {
        let format = try #require(AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: rate, channels: 1, interleaved: false))
        try writer.begin(format: format)
        let frames = Int(rate*0.273)
        let buffer = try #require(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frames)))
        buffer.frameLength = AVAudioFrameCount(frames)
        let channel = try #require(buffer.floatChannelData?[0])
        for index in 0..<frames { channel[index] = Float(sin(Double(index)*0.1)*0.2) }
        try writer.append(buffer)
        try writer.finishPart(kind: "paused")
        expected += Int64((Double(frames)*16000/rate).rounded())
        #expect(writer.samples == expected)
        #expect(throws: (any Error).self) { try writer.append(buffer) }
    }
    try writer.close(); try writer.close()
    #expect(try Data(contentsOf: pcm).count == Int(expected)*2)
    let events = try String(contentsOf: journal, encoding: .utf8).split(separator: "\n").map {
        try #require(JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any])
    }
    #expect(events.compactMap { $0["kind"] as? String } == ["resumed", "paused", "resumed", "paused", "resumed", "paused", "stopped"])
    #expect((events[1]["sample"] as? Int) == (events[2]["sample"] as? Int))
    #expect((events[3]["sample"] as? Int) == (events[4]["sample"] as? Int))
    #expect((events.last?["sample"] as? Int) == Int(expected))
}

@Test func recordingWriteFailureCannotAdvanceSampleCounter() throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: directory) }
    let pcm = directory.appendingPathComponent("closed.pcm")
    FileManager.default.createFile(atPath: pcm.path, contents: nil)
    let handle = try FileHandle(forWritingTo: pcm)
    let writer = PCMRecordingWriter(output: handle, journal: directory.appendingPathComponent("events.jsonl"))
    let format = try #require(AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16000, channels: 1, interleaved: false))
    try writer.begin(format: format)
    try handle.close()
    let buffer = try #require(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 4096))
    buffer.frameLength = 4096
    memset(try #require(buffer.floatChannelData?[0]), 0, 4096*MemoryLayout<Float>.size)
    #expect(throws: (any Error).self) { try writer.append(buffer) }
    #expect(writer.samples == 0)
    try? writer.close()
}
