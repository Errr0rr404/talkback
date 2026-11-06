// talkback-helper: two small jobs for the talkback plugin on macOS.
//
// 1. Stop speech the moment any microphone turns on. Hex (and most dictation tools) open the mic only
//    while you hold the hotkey, so pressing it cuts Kokoro off.
// 2. Auto-Enter: after Hex pastes a dictation into Terminal or iTerm2, press Return, but only if Claude Code
//    is the foreground program of the front tab (never at a shell prompt, even with Claude suspended in the
//    background). Off with `/talkback autoenter off` ("autoenter": false in ~/.talkback/config.json).
//
// Only one copy runs at a time (a lock on ~/.talkback/helper.lock), so Return is never pressed twice.
import AppKit
import ApplicationServices
import CoreAudio
import Foundation

let home = FileManager.default.homeDirectoryForCurrentUser.path
let state = ProcessInfo.processInfo.environment["TALKBACK_HOME"] ?? home + "/.talkback"
let historyPath = home + "/Library/Application Support/hex/history.json"
let configPath = state + "/config.json"
let endpointPath = state + "/voiced.json"
let ttyScripts = [
    "com.apple.Terminal": "tell application \"Terminal\" to get tty of selected tab of front window",
    "com.googlecode.iterm2": "tell application \"iTerm2\" to get tty of current session of current window",
]

func log(_ s: String) {
    FileHandle.standardError.write(("\(Date()) \(s)\n").data(using: .utf8)!)
}

// MARK: - Mic watch

func inputDevices() -> [AudioDeviceID] {
    var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDevices,
                                          mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size)
    var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
    AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &ids)
    return ids.filter { id in
        var sa = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyStreams,
                                            mScope: kAudioDevicePropertyScopeInput, mElement: kAudioObjectPropertyElementMain)
        var ss: UInt32 = 0
        AudioObjectGetPropertyDataSize(id, &sa, 0, nil, &ss)
        return ss > 0
    }
}

func micInUse() -> Bool {
    inputDevices().contains { id in
        var addr = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyDeviceIsRunningSomewhere,
                                              mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        var running: UInt32 = 0
        var size = UInt32(MemoryLayout<UInt32>.size)
        return AudioObjectGetPropertyData(id, &addr, 0, nil, &size, &running) == noErr && running != 0
    }
}

func stopSpeech() {
    // The daemon listens on 127.0.0.1; its port and token are in voiced.json.
    guard let data = FileManager.default.contents(atPath: endpointPath),
          let ep = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let port = ep["port"] as? Int, let token = ep["token"] as? String else { return }
    let fd = socket(AF_INET, SOCK_STREAM, 0)
    guard fd >= 0 else { return }
    defer { close(fd) }
    var addr = sockaddr_in()
    addr.sin_family = sa_family_t(AF_INET)
    addr.sin_port = in_port_t(UInt16(port).bigEndian)
    addr.sin_addr.s_addr = inet_addr("127.0.0.1")
    let ok = withUnsafePointer(to: &addr) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) }
    }
    guard ok == 0 else { return }  // daemon not running: nothing to stop
    let msg = "{\"cmd\": \"stop\", \"token\": \"\(token)\"}\n"
    _ = msg.withCString { write(fd, $0, strlen($0)) }
}

// MARK: - Auto-Enter

func config() -> [String: Any] {
    guard let data = FileManager.default.contents(atPath: configPath),
          let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return [:] }
    return obj
}

func entries() -> [[String: Any]] {
    guard let data = FileManager.default.contents(atPath: historyPath),
          let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let list = obj["entries"] as? [[String: Any]] else { return [] }
    return list
}

func intField(_ e: [String: Any], _ k: String) -> Int {
    if let s = e[k] as? String { return Int(s) ?? 0 }
    return (e[k] as? Int) ?? 0
}

func frontTabRunsClaude(_ bundle: String) -> Bool {
    guard let script = ttyScripts[bundle] else { return false }
    var err: NSDictionary?
    guard let tty = NSAppleScript(source: script)?.executeAndReturnError(&err).stringValue else {
        log("could not read the front tab's tty from \(bundle): \(err ?? [:])")
        return false
    }
    let ps = Process()
    ps.executableURL = URL(fileURLWithPath: "/bin/ps")
    ps.arguments = ["-t", (tty as NSString).lastPathComponent, "-o", "stat=,args="]
    let pipe = Pipe()
    ps.standardOutput = pipe
    guard (try? ps.run()) != nil else { return false }
    ps.waitUntilExit()
    let out = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
    return out.split(separator: "\n").contains { line in
        let fields = line.split(separator: " ", omittingEmptySubsequences: true).map(String.init)
        // "+" in STAT marks the terminal's foreground process group.
        guard fields.count >= 2, fields[0].contains("+") else { return false }
        let argv0 = (fields[1] as NSString).lastPathComponent
        // Native installs run as `claude`; npm installs as `node <...>/@anthropic-ai/claude-code/cli.js`.
        return argv0 == "claude"
            || (["node", "bun"].contains(argv0) && fields.count >= 3 && fields[2].contains("claude-code"))
    }
}

func pressReturn() {
    let src = CGEventSource(stateID: .hidSystemState)
    for down in [true, false] {
        let ev = CGEvent(keyboardEventSource: src, virtualKey: 36, keyDown: down)
        ev?.flags = []
        ev?.post(tap: .cghidEventTap)
    }
}

// MARK: - Main

let lockFd = open(state + "/helper.lock", O_CREAT | O_RDWR, 0o600)
guard lockFd >= 0, flock(lockFd, LOCK_EX | LOCK_NB) == 0 else {
    log("another talkback-helper is already running; exiting")
    exit(0)
}

// Accessibility is needed to post the Return key; only ask for it when auto-Enter is on.
let autoEnterOn = (config()["autoenter"] as? Bool) ?? true
let trusted = AXIsProcessTrustedWithOptions(["AXTrustedCheckOptionPrompt": autoEnterOn] as CFDictionary)
log("started; accessibility trusted=\(trusted)")

var micWasOn = micInUse()
let micTimer = DispatchSource.makeTimerSource(queue: .main)
micTimer.schedule(deadline: .now(), repeating: .milliseconds(80))
micTimer.setEventHandler {
    let on = micInUse()
    if on && !micWasOn {
        stopSpeech()
    }
    micWasOn = on
}
micTimer.resume()

var lastId = entries().map { intField($0, "id") }.max() ?? 0
var lastMtime = (try? FileManager.default.attributesOfItem(atPath: historyPath)[.modificationDate] as? Date) ?? nil

let hexTimer = DispatchSource.makeTimerSource(queue: .main)
hexTimer.schedule(deadline: .now(), repeating: .milliseconds(100))
hexTimer.setEventHandler {
    let mtime = (try? FileManager.default.attributesOfItem(atPath: historyPath)[.modificationDate] as? Date) ?? nil
    guard mtime != lastMtime else { return }
    lastMtime = mtime
    let fresh = entries().filter { intField($0, "id") > lastId }
    guard let newest = fresh.max(by: { intField($0, "id") < intField($1, "id") }) else { return }
    lastId = intField(newest, "id")

    let nowMs = Int(Date().timeIntervalSince1970 * 1000)
    guard (config()["autoenter"] as? Bool) ?? true,
          (newest["kind"] as? String) == "dictation",
          !((newest["final_text"] as? String) ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
          nowMs - intField(newest, "timestamp_ms") < 5000,
          let bundle = NSWorkspace.shared.frontmostApplication?.bundleIdentifier,
          ttyScripts[bundle] != nil
    else { return }
    guard frontTabRunsClaude(bundle) else {
        log("dictation \(lastId): front tab is not running claude, not pressing Return")
        return
    }
    // Let the paste land in the TUI before submitting.
    DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(150)) {
        pressReturn()
        log("dictation \(lastId): pressed Return")
    }
}
hexTimer.resume()
RunLoop.main.run()
