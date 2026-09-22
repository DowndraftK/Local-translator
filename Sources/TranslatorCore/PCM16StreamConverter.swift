@preconcurrency import AVFoundation
import Foundation

/// Stateful resampling across microphone buffers; call only from one serial queue.
public final class PCM16StreamConverter {
    private let converter: AVAudioConverter
    private let outputFormat: AVAudioFormat
    private let inputRate: Double
    private var ended = false
    private var inputFrames: Int64 = 0
    private var emittedFrames = 0
    private var pendingOutput = Data()

    public init(inputFormat: AVAudioFormat) throws {
        guard inputFormat.sampleRate > 0, inputFormat.channelCount > 0,
              let output = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: true),
              let converter = AVAudioConverter(from: inputFormat, to: output) else {
            throw M0Error.invalid("无法创建麦克风 PCM 转换器。")
        }
        self.converter = converter; self.outputFormat = output; self.inputRate = inputFormat.sampleRate
        converter.primeMethod = .none
    }

    public func convert(_ input: AVAudioPCMBuffer) throws -> Data {
        guard !ended else { throw M0Error.invalid("音频转换器已经结束。") }
        inputFrames += Int64(input.frameLength)
        let capacity = AVAudioFrameCount(ceil(Double(input.frameLength) * 16000 / inputRate)) + 256
        guard let output = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: capacity) else {
            throw M0Error.invalid("无法创建音频输出缓冲。")
        }
        var supplied = false
        var error: NSError?
        let status = converter.convert(to: output, error: &error) { _, state in
            if supplied { state.pointee = .noDataNow; return nil }
            supplied = true; state.pointee = .haveData; return input
        }
        if let error { throw error }
        guard status != .error else { throw M0Error.invalid("麦克风采样率转换失败。") }
        pendingOutput.append(bytes(output))
        return drain(to: Int(floor(Double(inputFrames) * 16000 / inputRate)))
    }

    public func finish() throws -> Data {
        guard !ended else { return Data() }
        ended = true
        for _ in 0..<16 {
            guard let output = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: 1024) else {
                throw M0Error.invalid("无法创建音频尾部缓冲。")
            }
            var error: NSError?
            let status = converter.convert(to: output, error: &error) { _, state in
                state.pointee = .endOfStream; return nil
            }
            if let error { throw error }
            guard status != .error else { throw M0Error.invalid("音频尾部转换失败。") }
            pendingOutput.append(bytes(output))
            if status == .endOfStream || output.frameLength == 0 {
                // AVAudioConverter may emit the filter's trailing padding. Keep
                // exactly the input duration; carry fractional frames between chunks.
                let result = drain(to: Int((Double(inputFrames) * 16000 / inputRate).rounded()))
                pendingOutput.removeAll()
                return result
            }
        }
        throw M0Error.invalid("音频转换器未能完成尾部收尾。")
    }

    private func drain(to targetFrames: Int) -> Data {
        let count = min(pendingOutput.count, max(0, targetFrames-emittedFrames) * 2)
        let data = Data(pendingOutput.prefix(count))
        pendingOutput.removeFirst(count)
        emittedFrames += count/2
        return data
    }

    private func bytes(_ buffer: AVAudioPCMBuffer) -> Data {
        guard buffer.frameLength > 0, let pointer = buffer.audioBufferList.pointee.mBuffers.mData else { return Data() }
        return Data(bytes: pointer, count: Int(buffer.frameLength) * 2)
    }
}
