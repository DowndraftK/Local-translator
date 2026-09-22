import AVFoundation
import Foundation
import Testing
@testable import TranslatorCore

@Test(arguments: [44100.0, 48000.0])
func microphoneConverterPreservesContinuousSamplesAndEOFTail(rate: Double) throws {
    let format = try #require(AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: rate, channels: 1, interleaved: false))
    let converter = try PCM16StreamConverter(inputFormat: format)
    let inputFrames = Int(rate * 2)
    var offset = 0
    var data = Data()
    while offset < inputFrames {
        let frames = min([511, 4096, 733][offset % 3], inputFrames-offset)
        let buffer = try #require(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frames)))
        buffer.frameLength = AVAudioFrameCount(frames)
        let channel = try #require(buffer.floatChannelData?[0])
        for index in 0..<frames {
            channel[index] = Float(sin(2 * .pi * 440 * Double(offset+index) / rate) * 0.25)
        }
        data.append(try converter.convert(buffer))
        offset += frames
    }
    data.append(try converter.finish())
    #expect(abs(data.count/2 - 32000) <= 2)
    #expect(try converter.finish().isEmpty)
    let rms = data.withUnsafeBytes { raw -> Double in
        let values = raw.bindMemory(to: Int16.self)
        return sqrt(values.reduce(0.0) { $0 + pow(Double($1)/32768, 2) } / Double(values.count))
    }
    #expect(rms > 0.16 && rms < 0.19)
}
