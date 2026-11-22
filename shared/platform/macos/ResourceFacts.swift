// Framework availability only: no file audio, device capture, playback or models.
import Foundation
import AVFoundation
import CoreML
import Metal

let result: [String: Any] = [
    "version": "mac-resource-facts-v1",
    "os": ProcessInfo.processInfo.operatingSystemVersionString,
    "avfoundation": NSClassFromString("AVAudioPlayer") != nil && NSClassFromString("AVAudioFile") != nil,
    "coreml_framework": NSClassFromString("MLModel") != nil,
    "metal_device": MTLCreateSystemDefaultDevice()?.name as Any? ?? NSNull(),
    "audio_opened": false, "playback": false, "capture": false, "model_load": false
]
let data = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
FileHandle.standardOutput.write(data)
FileHandle.standardOutput.write(Data([10]))
