@preconcurrency import AVFoundation
import Foundation
import TranslatorCore

/// Tap removal and engine transitions run on the main actor. The tap holds the
/// lock through enqueueing, so pause/stop cannot overtake an already copied buffer.
final class MicrophoneCapture: @unchecked Sendable {
    private let engine = AVAudioEngine()
    private let queue = DispatchQueue(label: "local.translator.microphone")
    private let lock = NSLock()
    private let writer: PCMRecordingWriter
    private let onError: @Sendable (String) -> Void
    private let onInterruption: @Sendable () -> Void
    private var queuedBuffers = 0
    private var accepting = false
    private var writeError = false
    private var tapInstalled = false
    private var closed = false
    private var observer: NSObjectProtocol?

    init(handle: FileHandle, journal: URL, onError: @escaping @Sendable (String) -> Void,
         onInterruption: @escaping @Sendable () -> Void) {
        writer = PCMRecordingWriter(output: handle, journal: journal)
        self.onError = onError; self.onInterruption = onInterruption
    }

    @MainActor func start() throws {
        guard !closed, !tapInstalled else { return }
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            throw NSError(domain: "Microphone", code: 1, userInfo: [NSLocalizedDescriptionKey: "没有可用麦克风，请连接设备后继续。"])
        }
        try writer.begin(format: format)
        lock.lock(); accepting = true; writeError = false; lock.unlock()
        input.installTap(onBus: 0, bufferSize: 4096, format: format) { [weak self] buffer, _ in
            guard let self else { return }
            self.lock.lock()
            guard self.accepting, !self.writeError else { self.lock.unlock(); return }
            guard self.queuedBuffers < 24 else {
                self.lock.unlock()
                self.failed("麦克风发送积压，录音已停止。请查看已保存录音，再继续识别。")
                return
            }
            guard let copy = AVAudioPCMBuffer(pcmFormat: buffer.format, frameCapacity: buffer.frameLength) else {
                self.lock.unlock(); self.failed("无法复制麦克风缓冲。"); return
            }
            copy.frameLength = buffer.frameLength
            let source = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: buffer.audioBufferList))
            let target = UnsafeMutableAudioBufferListPointer(copy.mutableAudioBufferList)
            for index in 0..<source.count {
                if let from = source[index].mData, let to = target[index].mData {
                    memcpy(to, from, Int(source[index].mDataByteSize))
                }
            }
            self.queuedBuffers += 1
            self.queue.async {
                defer { self.lock.lock(); self.queuedBuffers -= 1; self.lock.unlock() }
                do { try self.writer.append(copy) }
                catch { self.failed("麦克风音频保存失败：\(error.localizedDescription)") }
            }
            self.lock.unlock()
        }
        tapInstalled = true
        engine.prepare()
        do { try engine.start() }
        catch {
            detachTap()
            queue.async { try? self.writer.finishPart(kind: "interrupted") }
            throw error
        }
        if observer == nil {
            observer = NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange,
                object: engine, queue: nil) { [weak self] _ in self?.onInterruption() }
        }
    }

    private func failed(_ message: String) {
        lock.lock(); let first = !writeError; writeError = true; accepting = false; lock.unlock()
        if first { onError(message) }
    }

    @MainActor private func detachTap() {
        lock.lock(); accepting = false; lock.unlock()
        // Configuration changes can stop the engine before this callback runs.
        if tapInstalled { engine.inputNode.removeTap(onBus: 0); tapInstalled = false }
        engine.stop()
    }

    @MainActor func pause(interrupted: Bool, completion: @escaping @Sendable () -> Void) {
        guard !closed else { completion(); return }
        detachTap()
        queue.async {
            do { try self.writer.finishPart(kind: interrupted ? "interrupted" : "paused") }
            catch { self.failed("录音暂停保存失败：\(error.localizedDescription)") }
            completion()
        }
    }

    @MainActor func stop() {
        guard !closed else { return }
        closed = true
        detachTap()
        if let observer { NotificationCenter.default.removeObserver(observer); self.observer = nil }
        queue.async {
            do { try self.writer.close() }
            catch { self.failed("麦克风尾部保存失败：\(error.localizedDescription)") }
        }
    }
}
