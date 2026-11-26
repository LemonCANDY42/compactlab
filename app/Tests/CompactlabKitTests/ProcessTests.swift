import Foundation
import Testing

@testable import CompactlabKit

struct ProcessRunnerTests {
    @Test func outputAndStatus() async throws {
        let out = try await ProcessRunner.run(
            URL(fileURLWithPath: "/bin/sh"), ["-c", "echo hi; echo oops >&2; exit 3"], timeout: .seconds(10))
        #expect(out.status == 3)
        #expect(String(decoding: out.stdout, as: UTF8.self) == "hi\n")
        #expect(out.stderrTail == "oops")
        #expect(!out.timedOut && !out.cancelled)
    }

    @Test func largeOutputDoesNotStall() async throws {
        // Far more than a pipe buffer on both streams at once.
        let out = try await ProcessRunner.run(
            URL(fileURLWithPath: "/bin/sh"),
            ["-c", "head -c 1000000 /dev/zero | tr '\\0' a; head -c 300000 /dev/zero | tr '\\0' b >&2"],
            timeout: .seconds(20))
        #expect(out.stdout.count == 1_000_000)
        #expect(out.stderr.count == 300_000)
    }

    @Test func timeout() async throws {
        let start = ContinuousClock.now
        let out = try await ProcessRunner.run(URL(fileURLWithPath: "/bin/sleep"), ["30"], timeout: .milliseconds(300))
        #expect(out.timedOut)
        #expect(ContinuousClock.now - start < .seconds(5))
    }

    @Test func cancellation() async throws {
        let task = Task {
            try await ProcessRunner.run(URL(fileURLWithPath: "/bin/sleep"), ["30"], timeout: .seconds(60))
        }
        try await Task.sleep(for: .milliseconds(200))
        task.cancel()
        let out = try await task.value
        #expect(out.cancelled)
    }

    @Test func missingExecutableThrows() async {
        await #expect(throws: (any Error).self) {
            try await ProcessRunner.run(URL(fileURLWithPath: "/nonexistent/compactlab"), [], timeout: .seconds(5))
        }
    }
}

struct WatchProcessTests {
    @Test func linesInOrderThenExit() async throws {
        let tmp = try TempDir()
        let script = try tmp.script(
            "w",
            """
            echo '{"a":1}'
            while read cmd; do
              case "$cmd" in
                ping) printf '{"pong":1}\\n{"b":'; printf '2}\\n' ;;
                quit) printf 'tail-without-newline'; exit 4 ;;
              esac
            done
            """)
        let w = WatchProcess(executable: script, arguments: [])
        try w.start()
        var it = w.events.makeAsyncIterator()
        var e: WatchProcess.Event?
        e = await it.next()
        #expect(e == .line(Data(#"{"a":1}"#.utf8)))
        w.send("ping")
        e = await it.next()
        #expect(e == .line(Data(#"{"pong":1}"#.utf8)))
        e = await it.next()
        #expect(e == .line(Data(#"{"b":2}"#.utf8)))
        w.send("quit")
        e = await it.next()
        #expect(e == .line(Data("tail-without-newline".utf8)))
        e = await it.next()
        #expect(e == .exited(status: 4))
        e = await it.next()
        #expect(e == nil)
    }

    @Test func closingStdinEndsTheEngine() async throws {
        let tmp = try TempDir()
        let script = try tmp.script("w", "while read cmd; do :; done; exit 0")
        let w = WatchProcess(executable: script, arguments: [])
        try w.start()
        w.stop()
        var last: WatchProcess.Event?
        for await e in w.events { last = e }
        #expect(last == .exited(status: 0))
        w.send("refresh")  // gone: a no-op, not a crash (SIGPIPE)
    }

    @Test func stderrIsKept() async throws {
        let tmp = try TempDir()
        let script = try tmp.script("w", "echo 'Traceback: boom' >&2; exit 1")
        let w = WatchProcess(executable: script, arguments: [])
        try w.start()
        for await _ in w.events {}
        #expect(w.lastStderr.contains("boom"))
    }

    @Test func restartPolicy() {
        let t = Date(timeIntervalSince1970: 0)
        var p = RestartPolicy(window: 60, maxExits: 3)
        let burst = [0.0, 10, 20].map { p.shouldRestart(at: t.addingTimeInterval($0)) }
        #expect(burst == [true, true, false])
        var q = RestartPolicy(window: 60, maxExits: 3)
        let spread = [0.0, 30, 100].map { q.shouldRestart(at: t.addingTimeInterval($0)) }
        #expect(spread == [true, true, true])  // the first exit has left the window by the third
    }
}

/// The model against a fake engine that answers with the contract fixtures.
@MainActor
struct AppModelTests {
    final class FakeEngine {
        let tmp: TempDir
        let exe: URL
        var calls: [String] {
            (try? String(contentsOf: tmp.url.appendingPathComponent("calls"), encoding: .utf8))?
                .split(separator: "\n").map(String.init) ?? []
        }

        func set(_ flag: String, _ on: Bool) throws {
            let f = tmp.url.appendingPathComponent(flag)
            if on { try Data().write(to: f) } else { try? FileManager.default.removeItem(at: f) }
        }

        init() throws {
            tmp = try TempDir()
            for name in ["hello", "sessions", "result", "error", "plan", "applied", "reverted", "history", "verification",
                         "consent"] {
                _ = try tmp.write("\(name).json", try Fixture.line(name) + "\n")
            }
            var hello = try JSONSerialization.jsonObject(with: Fixture.data("hello")) as! [String: Any]
            hello["consent"] = (try JSONSerialization.jsonObject(with: Fixture.data("consent")) as! [String: Any])["consent"]
            _ = try tmp.write(
                "hello-consent.json",
                String(decoding: try JSONSerialization.data(withJSONObject: hello), as: UTF8.self) + "\n")
            _ = try tmp.write(
                "changed.json", #"{"engine_api": 1, "kind": "error", "error": {"code": "plan_changed", "message": "no"}}"# + "\n")
            _ = try tmp.write(
                "busy.json", #"{"engine_api": 1, "kind": "error", "error": {"code": "busy", "message": "held by pid 1"}}"# + "\n")
            let d = tmp.url.path
            exe = try tmp.script(
                "compactlab",
                """
                D='\(d)'
                echo "$*" >> "$D/calls"
                case "$2" in
                  hello) if [ -e "$D/consent" ]; then cat "$D/hello-consent.json"; else cat "$D/hello.json"; fi ;;
                  plan) cat "$D/plan.json" ;;
                  apply) if [ -e "$D/plan-changed" ]; then cat "$D/changed.json"; exit 1; fi; touch "$D/applied"; cat "$D/applied.json" ;;
                  revert) cat "$D/reverted.json" ;;
                  history) if [ -e "$D/applied" ]; then cat "$D/history.json"; else echo '{"engine_api":1,"kind":"history","applies":[]}'; fi ;;
                  verify) cat "$D/verification.json" ;;
                  consent) if [ "$3" = set ]; then touch "$D/consent"; else rm -f "$D/consent"; fi; cat "$D/consent.json" ;;
                  result) if [ -e "$D/no-result" ]; then cat "$D/error.json"; exit 1; else cat "$D/result.json"; fi ;;
                  calibrate) if [ -e "$D/slow" ]; then sleep 3; fi; sleep 0.2; if [ -e "$D/busy" ]; then cat "$D/busy.json"; exit 1; fi; rm -f "$D/no-result"; cat "$D/result.json" ;;
                  watch)
                    if [ -e "$D/crash" ]; then echo 'Traceback: watch broke' >&2; exit 1; fi
                    cat "$D/hello.json"; cat "$D/sessions.json"
                    while read cmd; do
                      case "$cmd" in
                        quit) exit 0 ;;
                        refresh|snapshot) cat "$D/sessions.json" ;;
                        *) echo 'not json' ;;
                      esac
                    done ;;
                esac
                """)
        }
    }

    func model(_ engine: FakeEngine) -> AppModel {
        let m = AppModel(
            defaults: UserDefaults(suiteName: "compactlab-tests-\(UUID().uuidString)")!,
            environment: ["COMPACTLAB_ENGINE": engine.exe.path, "PATH": "/usr/bin:/bin"])
        m.scheduling = false
        return m
    }

    @Test func startsAndShowsSessionsAndThresholds() async throws {
        let engine = try FakeEngine()
        let m = model(engine)
        await m.start()
        defer { m.shutdown() }
        #expect(m.status == .ready(version: "0.1.0"))
        #expect(m.result?.groups.count == 5)
        #expect(await eventually { m.sessions != nil })
        #expect(m.sessionRows.count == 3)
        #expect(m.watchStatus == .running)
        #expect(!m.thresholdRows.isEmpty)
    }

    @Test func missingEngine() async throws {
        let m = AppModel(
            defaults: UserDefaults(suiteName: "compactlab-tests-\(UUID().uuidString)")!,
            environment: ["PATH": "/nonexistent"])
        m.scheduling = false
        // Only the default install places are left; this machine may have one, so point HOME-based search elsewhere.
        m.enginePath = ""
        await m.start()
        if m.executable == nil {
            #expect(m.status == .missing)
            #expect(m.icon == .engineProblem)
        }
        m.shutdown()
    }

    @Test func calibrateWhenDue() async throws {
        let engine = try FakeEngine()
        try engine.set("no-result", true)
        let m = model(engine)
        m.scheduling = true
        m.powerState = { PowerState(lowPowerMode: false, onBattery: false, batteryPercent: nil) }
        await m.start()  // the fixture's service has never calibrated: due now
        defer { m.shutdown() }
        #expect(await eventually { engine.calls.contains { $0.hasPrefix("engine calibrate --reason app-schedule") } })
        #expect(await eventually { m.result != nil && !m.isCalibrating })
        #expect(m.calibration == .idle)
    }

    @Test func lowPowerPutsOffTheSchedule() async throws {
        let engine = try FakeEngine()
        let m = model(engine)
        m.scheduling = true
        m.powerState = { PowerState(lowPowerMode: true, onBattery: true, batteryPercent: 50) }
        await m.start()
        defer { m.shutdown() }
        #expect(!engine.calls.contains { $0.hasPrefix("engine calibrate") })
    }

    @Test func busyIsNotAFailure() async throws {
        let engine = try FakeEngine()
        try engine.set("busy", true)
        let m = model(engine)
        await m.start()
        defer { m.shutdown() }
        m.recalibrate()
        #expect(await eventually { !m.isCalibrating })
        #expect(m.calibration == .busy("held by pid 1"))
    }

    @Test func watchCrashesRestartThenStop() async throws {
        let engine = try FakeEngine()
        try engine.set("crash", true)
        let m = model(engine)
        await m.start()
        defer { m.shutdown() }
        #expect(await eventually(timeout: .seconds(15)) { m.isWatchStopped })
        #expect(engine.calls.filter { $0 == "engine watch" }.count == 3)
        guard case .stopped(let why) = m.watchStatus else { return }
        #expect(why.contains("watch broke"))
        #expect(m.icon == .engineProblem)
        // The user restarts it once the engine is fixed.
        try engine.set("crash", false)
        m.restartWatch()
        #expect(await eventually { m.watchStatus == .running && m.sessions != nil })
    }

    @Test func hidingProjectNamesRestartsWatchWithTheFlag() async throws {
        let engine = try FakeEngine()
        let m = model(engine)
        await m.start()
        defer { m.shutdown() }
        #expect(await eventually { m.sessions != nil })
        m.showProjects = false
        #expect(await eventually { engine.calls.contains("engine watch --no-project-names") })
        #expect(await eventually { m.watchStatus == .running })
    }

    @Test func incompatibleEngineIsReadOnly() async throws {
        let engine = try FakeEngine()
        let hello = try engine.tmp.url.appendingPathComponent("hello.json")
        try String(contentsOf: hello, encoding: .utf8).replacingOccurrences(of: #""engine_api":1"#, with: #""engine_api":2"#)
            .write(to: hello, atomically: true, encoding: .utf8)
        let m = model(engine)
        m.scheduling = true
        m.powerState = { PowerState(lowPowerMode: false, onBattery: false, batteryPercent: nil) }
        await m.start()
        defer { m.shutdown() }
        #expect(m.status == .incompatible(api: 2, version: "0.1.0"))
        #expect(m.result != nil)  // still shown
        m.recalibrate()
        await m.calibrateIfDue()
        #expect(!engine.calls.contains { $0.hasPrefix("engine calibrate") })
    }

    @Test func externalCalibrationPostponesTheSchedule() async throws {
        let engine = try FakeEngine()
        let m = model(engine)
        await m.start()  // scheduling off: the fixture's service has never calibrated
        defer { m.shutdown() }
        // Meanwhile the CLI calibrates.
        let hello = engine.tmp.url.appendingPathComponent("hello.json")
        let now = Int(Date().timeIntervalSince1970)
        try String(contentsOf: hello, encoding: .utf8)
            .replacingOccurrences(of: #""last_attempt":null,"last_calibrated":null"#, with: #""last_attempt":\#(now),"last_calibrated":\#(now)"#)
            .write(to: hello, atomically: true, encoding: .utf8)
        m.scheduling = true
        m.serviceFreshFor = 0
        m.powerState = { PowerState(lowPowerMode: false, onBattery: false, batteryPercent: nil) }
        await m.calibrateIfDue()
        #expect(m.hello?.service.lastCalibrated == Double(now))
        #expect(!engine.calls.contains { $0.hasPrefix("engine calibrate") })
    }

    @Test func interruptedCalibrationBacksOff() async throws {
        let engine = try FakeEngine()
        try engine.set("slow", true)
        let m = model(engine)
        m.calibrateTimeout = .milliseconds(800)  // the slow fake calibrate takes over 3 s
        m.powerState = { PowerState(lowPowerMode: false, onBattery: false, batteryPercent: nil) }
        await m.start()
        defer { m.shutdown() }
        m.scheduling = true
        await m.calibrateIfDue()
        #expect(m.isCalibrating)
        #expect(await eventually { !m.isCalibrating })
        #expect(m.calibration == .failed("the calibration ran too long and was stopped"))
        await m.calibrateIfDue()  // still due by the service state, but backing off: no run starts
        #expect(!m.isCalibrating && m.calibration == .failed("the calibration ran too long and was stopped"))
    }

    @Test func scheduleOffMeansManualOnly() async throws {
        let engine = try FakeEngine()
        let result = engine.tmp.url.appendingPathComponent("result.json")
        try String(contentsOf: result, encoding: .utf8).replacingOccurrences(of: #""every_s":[0-9.]+"#, with: #""every_s":null"#, options: .regularExpression)
            .write(to: result, atomically: true, encoding: .utf8)
        let m = model(engine)
        m.scheduling = true
        m.powerState = { PowerState(lowPowerMode: false, onBattery: false, batteryPercent: nil) }
        await m.start()  // never calibrated by the service state, but the last run turned the schedule off
        defer { m.shutdown() }
        #expect(m.result?.calibration != nil && m.result?.calibration?.everyS == nil)
        #expect(!engine.calls.contains { $0.hasPrefix("engine calibrate") })
    }

    @Test func profileReloadsTheResult() async throws {
        let engine = try FakeEngine()
        let m = model(engine)
        await m.start()
        defer { m.shutdown() }
        m.displayProfile = "cost"
        #expect(await eventually { engine.calls.contains("engine result --profile cost") })
    }
}

struct FileWatcherTests {
    final class Counter: @unchecked Sendable {
        private let lock = NSLock()
        private var n = 0
        func bump() { lock.withLock { n += 1 } }
        var value: Int { lock.withLock { n } }
    }

    @Test @MainActor func writesAreReported() async throws {
        let tmp = try TempDir()
        let dir = tmp.url.resolvingSymlinksInPath()
        let count = Counter()
        let w = FileWatcher { count.bump() }
        #expect(w.watch([dir.path], latency: 0.1))
        try await Task.sleep(for: .milliseconds(200))
        try Data("x".utf8).write(to: dir.appendingPathComponent("a.jsonl"))
        #expect(await eventually { count.value > 0 })
        w.stop()
        // A replaced watcher's stream holds its callback; freeing the watcher is safe.
        var other: FileWatcher? = FileWatcher { count.bump() }
        other?.watch([dir.path], latency: 0.1)
        other = nil
        try Data("y".utf8).write(to: dir.appendingPathComponent("b.jsonl"))
        try await Task.sleep(for: .milliseconds(300))
    }
}
