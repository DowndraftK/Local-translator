import Foundation
import AVFoundation

func emit(_ value: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]),
       let text = String(data: data, encoding: .utf8) { print(text); fflush(stdout) }
}

final class Playback: NSObject, AVAudioPlayerDelegate {
    var player: AVAudioPlayer?
    var done = false
    var buffer = Data()
    let owner = CommandLine.arguments[2]
    func report(_ event: String) {
        emit(["event": event, "session": owner, "position": player?.currentTime ?? 0,
              "duration": player?.duration ?? 0, "playing": player?.isPlaying ?? false])
    }
    func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        report(flag ? "ended" : "error"); done = true
    }
    func audioPlayerDecodeErrorDidOccur(_ player: AVAudioPlayer, error: Error?) {
        emit(["event": "error", "session": owner, "message": error?.localizedDescription ?? "播放解码失败"])
        done = true
    }
    func command(_ value: [String: Any]) {
        guard value["session"] as? String == owner else { return }
        switch value["action"] as? String {
        case "pause": player?.pause()
        case "resume":
            if player?.play() != true { report("error"); done = true }
        case "seek":
            guard let seconds = value["seconds"] as? Double, seconds.isFinite,
                  seconds >= 0, seconds < (player?.duration ?? 0) else { report("error"); done = true; return }
            player?.currentTime = seconds
        case "stop": player?.stop(); done = true
        default: return
        }
        report("position")
    }
}

let playback = Playback()
do {
    let player = try AVAudioPlayer(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))
    playback.player = player; player.delegate = playback
    guard let start = Double(CommandLine.arguments[3]), start.isFinite, start >= 0, start < player.duration else { throw NSError(domain: "T07Playback", code: 2) }
    player.currentTime = start
    guard player.prepareToPlay(), player.play() else { throw NSError(domain: "T07Playback", code: 1) }
    playback.report("started")
    let input = FileHandle.standardInput
    input.readabilityHandler = { handle in
        let data = handle.availableData
        DispatchQueue.main.async {
            if data.isEmpty { playback.done = true; return }
            playback.buffer.append(data)
            if playback.buffer.count > 65536 { playback.done = true; return }
            while let end = playback.buffer.firstIndex(of: 10) {
                let line = playback.buffer.prefix(upTo: end)
                playback.buffer.removeSubrange(...end)
                if let value = try? JSONSerialization.jsonObject(with: line) as? [String: Any] { playback.command(value) }
            }
        }
    }
    var last = Date.distantPast
    while !playback.done {
        RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.04))
        if Date().timeIntervalSince(last) >= 0.2 { playback.report("position"); last = Date() }
    }
    input.readabilityHandler = nil
    player.stop(); playback.report("stopped"); playback.player = nil
} catch {
    emit(["event": "error", "session": playback.owner, "message": error.localizedDescription])
    exit(1)
}
