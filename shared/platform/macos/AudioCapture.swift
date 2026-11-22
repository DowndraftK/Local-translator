@preconcurrency import AVFoundation
import AppKit
import AudioToolbox
import CoreAudio
import CoreGraphics
import ScreenCaptureKit
import Foundation

// The unchanged core converter is compiled into this helper as a second source.
enum M0Error: LocalizedError {
    case invalid(String)
    var errorDescription: String? { switch self { case let .invalid(message): return message } }
}

final class Events: @unchecked Sendable {
    private let lock = NSLock()
    let session: String
    init(_ session: String) { self.session = session }
    func send(_ kind: String, _ fields: [String: Any] = [:]) {
        lock.lock()
        defer { lock.unlock() }
        var value = fields
        value["event"] = kind
        value["session"] = session
        value["wall"] = Date().timeIntervalSince1970
        do {
            var bytes = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
            guard bytes.count < 65536 else { exit(3) }
            bytes.append(10)
            try FileHandle.standardOutput.write(contentsOf: bytes)
        } catch { exit(4) }
    }
}

func uint32Property(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> UInt32? {
    var address = AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var value: UInt32 = 0
    var size = UInt32(MemoryLayout<UInt32>.size)
    let status = AudioObjectGetPropertyData(object, &address, 0, nil, &size, &value)
    return status == noErr && size == MemoryLayout<UInt32>.size ? value : nil
}

func doubleProperty(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> Double? {
    var address = AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var value = 0.0
    var size = UInt32(MemoryLayout<Double>.size)
    let status = AudioObjectGetPropertyData(object, &address, 0, nil, &size, &value)
    return status == noErr && size == MemoryLayout<Double>.size ? value : nil
}

func stringProperty(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String? {
    var address = AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var value: Unmanaged<CFString>?
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    guard AudioObjectGetPropertyData(object, &address, 0, nil, &size, &value) == noErr else { return nil }
    return value?.takeRetainedValue() as String?
}

func deviceFacts(_ device: AudioDeviceID) -> [String: Any] {
    return ["id": device, "uid": stringProperty(device, kAudioDevicePropertyDeviceUID) ?? "",
            "name": stringProperty(device, kAudioObjectPropertyName) ?? "未知设备",
            "rate": doubleProperty(device, kAudioDevicePropertyNominalSampleRate) ?? 0,
            "alive": uint32Property(device, kAudioDevicePropertyDeviceIsAlive) == 1,
            "transport": uint32Property(device, kAudioDevicePropertyTransportType) ?? 0]
}

func inputDevices() -> [[String: Any]] {
    var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDevices,
        mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size) == noErr else { return [] }
    var devices = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
    guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &devices) == noErr else { return [] }
    return devices.compactMap { device in
        var input = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyStreams,
            mScope: kAudioDevicePropertyScopeInput, mElement: kAudioObjectPropertyElementMain)
        var bytes: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(device, &input, 0, nil, &bytes) == noErr, bytes > 0 else { return nil }
        return deviceFacts(device)
    }
}

func outputFacts() -> [String: Any] {
    let device = uint32Property(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDefaultOutputDevice) ?? 0
    return deviceFacts(device)
}

func microphonePermission() -> String {
    switch AVCaptureDevice.authorizationStatus(for: .audio) {
    case .authorized: return "authorized"
    case .denied: return "denied"
    case .restricted: return "restricted"
    case .notDetermined: return "not_determined"
    @unknown default: return "unknown"
    }
}

final class Capture: NSObject, SCStreamOutput, SCStreamDelegate, @unchecked Sendable {
    let events: Events
    let request: [String: Any]
    private let queue = DispatchQueue(label: "local.translator.capture.archive")
    private let callbackQueue = DispatchQueue(label: "local.translator.capture.system")
    private let lock = NSLock()
    private var accepting = false
    private var queued = 0
    private var failed = false
    private var converter: PCM16StreamConverter?
    private var frames: Int64 = 0
    private var samples: Int64 = 0
    private var sequence = 0
    private var epoch = 0
    private var firstHost: UInt64?
    private var firstSourceClock: [String: Any]?
    private var engine: AVAudioEngine?
    private var engineObserver: NSObjectProtocol?
    private var stream: SCStream?
    private var tap = false
    private var observers: [NSObjectProtocol] = []
    private var scopeFacts: [String: Any] = [:]
    private var closed = false
    private var paused = true
    private var monitor: Task<Void, Never>?
    private var lastDefaultInput: AudioDeviceID = 0
    private var lastOutput: AudioDeviceID = 0
    private var propertyPolls = 0

    init(_ request: [String: Any]) {
        self.request = request
        events = Events(request["session"] as! String)
        super.init()
    }

    @MainActor func begin() async throws {
        guard !closed, paused else { throw M0Error.invalid("采集会话状态已改变。") }
        epoch += 1
        events.send("permission", ["kind": request["kind"]!, "microphone": microphonePermission(),
            "systemAudio": CGPreflightScreenCaptureAccess() ? "authorized" : "not_authorized_or_not_determined",
            "helperBundle": Bundle.main.bundleIdentifier ?? "unbundled", "pid": ProcessInfo.processInfo.processIdentifier])
        if request["kind"] as? String == "microphone" {
            if microphonePermission() == "not_determined" {
                events.send("waiting_permission", ["permission": "microphone"])
                guard await AVCaptureDevice.requestAccess(for: .audio) else { throw M0Error.invalid("麦克风权限未允许，录音未开始。") }
            }
            guard microphonePermission() == "authorized" else { throw M0Error.invalid("麦克风权限未允许，录音未开始。") }
            try beginMicrophone()
        } else {
            events.send("waiting_permission", ["permission": "screen_and_system_audio"])
            try await beginSystemAudio()
        }
        paused = false
        if monitor == nil {
            lastDefaultInput = uint32Property(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDefaultInputDevice) ?? 0
            lastOutput = uint32Property(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDefaultOutputDevice) ?? 0
            monitor = Task { @MainActor [weak self] in
                while !Task.isCancelled {
                    try? await Task.sleep(nanoseconds: 1_000_000_000)
                    guard let self, !self.closed else { return }
                    if !self.paused { await self.checkRoute() }
                }
            }
        }
        if observers.isEmpty {
            observers.append(NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.willSleepNotification,
                object: nil, queue: .main) { [weak self] _ in
                Task { @MainActor in await self?.interrupted("system_sleep") }
            })
        }
    }

    @MainActor private func beginMicrophone() throws {
        guard let uid = request["deviceUID"] as? String,
              let chosen = inputDevices().first(where: { $0["uid"] as? String == uid }),
              let device = chosen["id"] as? UInt32, chosen["alive"] as? Bool == true else {
            throw M0Error.invalid("选定麦克风已不可用，请停止后重新选择设备。")
        }
        let engine = AVAudioEngine()
        let input = engine.inputNode
        guard let unit = input.audioUnit else { throw M0Error.invalid("麦克风输入单元不可用。") }
        var actual: AudioDeviceID = 0
        var bytes = UInt32(MemoryLayout<AudioDeviceID>.size)
        let initialStatus = AudioUnitGetProperty(unit, kAudioOutputUnitProperty_CurrentDevice,
            kAudioUnitScope_Global, 0, &actual, &bytes)
        if initialStatus != noErr || actual != device {
            var selected = device
            guard AudioUnitSetProperty(unit, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 0,
                &selected, UInt32(MemoryLayout<AudioDeviceID>.size)) == noErr else { throw M0Error.invalid("无法应用选定麦克风。") }
        }
        guard AudioUnitGetProperty(unit, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 0,
            &actual, &bytes) == noErr, actual == device else { throw M0Error.invalid("实际麦克风与选择不符。") }
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate >= 8000, format.sampleRate <= 192000, format.channelCount <= 8,
              format.channelCount > 0 else { throw M0Error.invalid("麦克风格式不受支持。") }
        converter = try PCM16StreamConverter(inputFormat: format)
        frames = 0
        firstHost = nil
        firstSourceClock = nil
        scopeFacts = ["kind": "microphone", "device": deviceFacts(actual),
            "inputRate": format.sampleRate, "channels": format.channelCount,
            "defaultInputID": uint32Property(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDefaultInputDevice) ?? 0]
        events.send("ready", ["epoch": epoch, "source": scopeFacts, "output": outputFacts(),
            "format": "16000Hz mono PCM16LE", "samples": samples])
        lock.withLock { accepting = true; failed = false }
        input.installTap(onBus: 0, bufferSize: 4096, format: format) { [weak self] buffer, when in
            self?.enqueue(buffer, host: when.isHostTimeValid ? when.hostTime : nil,
                clock: ["kind": "input_sample_time", "sampleTime": when.isSampleTimeValid ? when.sampleTime as Any : NSNull(),
                        "sampleRate": when.sampleRate])
        }
        tap = true
        self.engine = engine
        engine.prepare()
        try engine.start()
        engineObserver = NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange,
            object: engine, queue: .main) { [weak self] _ in
            Task { @MainActor in await self?.checkMicrophoneConfiguration() }
        }
    }

    @MainActor private func checkMicrophoneConfiguration() async {
        guard !paused, !closed, let engine else { return }
        let format = engine.inputNode.outputFormat(forBus: 0)
        if !engine.isRunning || format.sampleRate != scopeFacts["inputRate"] as? Double ||
            format.channelCount != scopeFacts["channels"] as? UInt32 {
            await interrupted("device_or_format_changed")
        }
    }

    @MainActor private func beginSystemAudio() async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(true, onScreenWindowsOnly: false)
        guard let display = content.displays.first else { throw M0Error.invalid("没有可用的系统声音捕获范围。") }
        let pids = (request["excludedPIDs"] as? [Int] ?? []) + [Int(ProcessInfo.processInfo.processIdentifier)]
        let excluded = content.applications.filter {
            pids.contains(Int($0.processID)) || $0.bundleIdentifier.hasPrefix("local.kevin.translator")
        }
        let filter = SCContentFilter(display: display, excludingApplications: excluded, exceptingWindows: [])
        let config = SCStreamConfiguration()
        config.capturesAudio = true
        config.captureMicrophone = false
        config.excludesCurrentProcessAudio = true
        config.sampleRate = 48000
        config.channelCount = 2
        config.width = 2
        config.height = 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        config.queueDepth = 3
        config.showsCursor = false
        let stream = SCStream(filter: filter, configuration: config, delegate: self)
        // No screen output is registered and no image/video buffer is archived.
        try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: callbackQueue)
        self.stream = stream
        frames = 0
        firstHost = nil
        firstSourceClock = nil
        converter = nil
        scopeFacts = ["kind": "system_audio", "scope": "all_capturable_system_audio", "displayID": display.displayID,
            "microphone": false, "screenOutput": false, "excludesCurrentProcessAudio": true,
            "excludedApplications": excluded.map { ["bundle": $0.bundleIdentifier, "pid": $0.processID] },
            "excludedPIDs": pids, "inputRate": 48000.0, "channels": UInt32(2)]
        events.send("ready", ["epoch": epoch, "source": scopeFacts, "output": outputFacts(),
            "format": "16000Hz mono PCM16LE", "samples": samples])
        lock.withLock { accepting = true; failed = false }
        try await stream.startCapture()
    }

    private func enqueue(_ buffer: AVAudioPCMBuffer, host: UInt64?, clock: [String: Any]) {
        lock.lock()
        guard accepting, !failed else { lock.unlock(); return }
        guard queued < 24, buffer.frameLength <= 65536,
              let copy = AVAudioPCMBuffer(pcmFormat: buffer.format, frameCapacity: buffer.frameLength) else {
            lock.unlock(); fail("capture_backpressure"); return
        }
        copy.frameLength = buffer.frameLength
        let source = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: buffer.audioBufferList))
        let target = UnsafeMutableAudioBufferListPointer(copy.mutableAudioBufferList)
        for index in 0..<source.count {
            guard index < target.count, source[index].mDataByteSize <= target[index].mDataByteSize,
                  let from = source[index].mData, let to = target[index].mData else {
                lock.unlock(); fail("capture_buffer_format"); return
            }
            memcpy(to, from, Int(source[index].mDataByteSize))
        }
        queued += 1
        let currentEpoch = epoch
        queue.async {
            defer { self.lock.lock(); self.queued -= 1; self.lock.unlock() }
            do {
                if self.converter == nil { self.converter = try PCM16StreamConverter(inputFormat: copy.format) }
                if self.firstHost == nil { self.firstHost = host }
                if self.firstSourceClock == nil { self.firstSourceClock = clock }
                self.frames += Int64(copy.frameLength)
                try self.write(self.converter!.convert(copy), epoch: currentEpoch, host: host,
                    rate: copy.format.sampleRate, channels: copy.format.channelCount, clock: clock)
            } catch { self.fail("capture_conversion " + error.localizedDescription) }
        }
        lock.unlock()
    }

    private func write(_ data: Data, epoch: Int, host: UInt64?, rate: Double, channels: UInt32, clock: [String: Any] = [:]) throws {
        var offset = 0
        while offset < data.count {
            let end = min(offset + 16000, data.count)
            let part = data.subdata(in: offset..<end)
            events.send("pcm", ["epoch": epoch, "sequence": sequence, "offset": samples,
                "samples": part.count / 2, "data": part.base64EncodedString(),
                "hostTime": host.map { $0 as Any } ?? NSNull(), "firstHostTime": firstHost.map { $0 as Any } ?? NSNull(),
                "hostSeconds": host.map { AVAudioTime.seconds(forHostTime: $0) as Any } ?? NSNull(),
                "sourceClock": clock, "firstSourceClock": firstSourceClock ?? [:],
                "inputFrames": frames, "inputRate": rate, "inputChannels": channels])
            samples += Int64(part.count / 2)
            sequence += 1
            offset = end
        }
    }

    private func fail(_ reason: String) {
        lock.lock(); let first = !failed; failed = true; accepting = false; lock.unlock()
        if first {
            DispatchQueue.global(qos: .utility).async {
                self.events.send("failure", ["reason": reason, "epoch": self.epoch])
            }
        }
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sample: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, sample.isValid, let description = sample.formatDescription else { return }
        let format = AVAudioFormat(cmAudioFormatDescription: description)
        let count = CMSampleBufferGetNumSamples(sample)
        guard count > 0, count <= 65536, let pcm = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(count)) else {
            fail("system_audio_buffer_limit"); return
        }
        pcm.frameLength = AVAudioFrameCount(count)
        var size = 0
        CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(sample, bufferListSizeNeededOut: &size,
            bufferListOut: nil, bufferListSize: 0, blockBufferAllocator: nil, blockBufferMemoryAllocator: nil,
            flags: UInt32(kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment), blockBufferOut: nil)
        let memory = UnsafeMutableRawPointer.allocate(byteCount: max(size, MemoryLayout<AudioBufferList>.size), alignment: 16)
        defer { memory.deallocate() }
        let list = memory.bindMemory(to: AudioBufferList.self, capacity: 1)
        var block: CMBlockBuffer?
        let status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(sample, bufferListSizeNeededOut: nil,
            bufferListOut: list, bufferListSize: size, blockBufferAllocator: nil, blockBufferMemoryAllocator: nil,
            flags: UInt32(kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment), blockBufferOut: &block)
        guard status == noErr else { fail("system_audio_buffer_copy"); return }
        let from = UnsafeMutableAudioBufferListPointer(list)
        let to = UnsafeMutableAudioBufferListPointer(pcm.mutableAudioBufferList)
        guard from.count == to.count else { fail("system_audio_channel_change"); return }
        for index in 0..<from.count {
            guard from[index].mDataByteSize <= to[index].mDataByteSize,
                  let source = from[index].mData, let target = to[index].mData else { fail("system_audio_format"); return }
            memcpy(target, source, Int(from[index].mDataByteSize))
        }
        let timestamp = CMSampleBufferGetPresentationTimeStamp(sample)
        enqueue(pcm, host: nil, clock: ["kind": "CMSampleBuffer_PTS", "value": timestamp.value,
            "timescale": timestamp.timescale, "epoch": timestamp.epoch, "valid": timestamp.isValid])
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        fail("system_audio_interrupted " + error.localizedDescription)
    }

    @MainActor func interrupted(_ reason: String) async {
        guard !paused, !closed else { return }
        do { try await pause("interrupted", control: reason) }
        catch { events.send("failure", ["reason": error.localizedDescription]) }
    }

    @MainActor private func checkRoute() async {
        propertyPolls += 1
        let input = uint32Property(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDefaultInputDevice) ?? 0
        let output = uint32Property(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDefaultOutputDevice) ?? 0
        if let device = scopeFacts["device"] as? [String: Any], let selected = device["id"] as? UInt32,
           uint32Property(selected, kAudioDevicePropertyDeviceIsAlive) != 1 {
            await interrupted("selected_device_removed")
            return
        }
        if input != lastDefaultInput || output != lastOutput {
            events.send("route_changed", ["epoch": epoch, "defaultInputID": input, "output": outputFacts(),
                "selectedSource": scopeFacts, "propertyPolls": propertyPolls])
            lastDefaultInput = input
            lastOutput = output
        }
    }

    @MainActor func pause(_ kind: String = "paused", control: String) async throws {
        guard !closed else { return }
        paused = true
        lock.withLock { accepting = false }
        if let engineObserver {
            NotificationCenter.default.removeObserver(engineObserver)
            self.engineObserver = nil
        }
        if tap { engine?.inputNode.removeTap(onBus: 0); tap = false }
        engine?.stop()
        if let stream { try await stream.stopCapture(); self.stream = nil }
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            queue.async {
                do {
                    if let converter = self.converter {
                        try self.write(converter.finish(), epoch: self.epoch, host: nil,
                            rate: (self.scopeFacts["inputRate"] as? Double) ?? 48000,
                            channels: (self.scopeFacts["channels"] as? UInt32) ?? 1)
                    }
                    self.converter = nil
                    self.events.send(kind, ["epoch": self.epoch, "samples": self.samples, "inputFrames": self.frames,
                        "control": control, "output": outputFacts(), "source": self.scopeFacts, "propertyPolls": self.propertyPolls])
                    continuation.resume()
                } catch { continuation.resume(throwing: error) }
            }
        }
        engine = nil
    }

    @MainActor func stop(control: String) async throws {
        try await pause("stopped", control: control)
        closed = true
        monitor?.cancel()
        monitor = nil
        for observer in observers {
            NotificationCenter.default.removeObserver(observer)
            NSWorkspace.shared.notificationCenter.removeObserver(observer)
        }
        observers.removeAll()
    }
}

@main struct AudioCaptureMain {
    @MainActor static func main() async {
        signal(SIGPIPE, SIG_IGN)
        guard CommandLine.arguments.count == 2 else { exit(2) }
        do {
            let raw = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))
            guard raw.count <= 16384, let request = try JSONSerialization.jsonObject(with: raw) as? [String: Any],
                  let session = request["session"] as? String else { exit(2) }
            let events = Events(session)
            if request["action"] as? String == "describe" {
                events.send("description", ["devices": inputDevices(), "microphonePermission": microphonePermission(),
                    "systemAudioPermission": CGPreflightScreenCaptureAccess() ? "authorized" : "not_authorized_or_not_determined",
                    "output": outputFacts(), "systemAudioAPI": "ScreenCaptureKit", "microphoneOpened": false,
                    "systemAudioOpened": false, "bundle": Bundle.main.bundleIdentifier ?? "unbundled"])
                return
            }
            let capture = Capture(request)
            do { try await capture.begin() }
            catch {
                events.send("failure", ["reason": error.localizedDescription, "started": false,
                    "microphonePermission": microphonePermission(),
                    "systemAudioPermission": CGPreflightScreenCaptureAccess() ? "authorized" : "not_authorized_or_not_determined"])
                try? await capture.stop(control: "start_failed")
                exit(1)
            }
            for try await line in FileHandle.standardInput.bytes.lines {
                guard line.utf8.count <= 16384,
                      let command = try JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any],
                      command["session"] as? String == session, let control = command["control"] as? String else {
                    throw M0Error.invalid("采集控制归属不匹配。")
                }
                switch command["action"] as? String {
                case "pause": try await capture.pause(control: control)
                case "resume": try await capture.begin()
                case "stop": try await capture.stop(control: control); return
                default: throw M0Error.invalid("采集控制动作无效。")
                }
            }
            try await capture.stop(control: "parent_pipe_closed")
        } catch {
            FileHandle.standardError.write(Data((error.localizedDescription + "\n").utf8))
            exit(1)
        }
    }
}
