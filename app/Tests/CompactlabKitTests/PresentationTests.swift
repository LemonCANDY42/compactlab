import Foundation
import Testing

@testable import CompactlabKit

struct FormatTests {
    @Test func tokens() {
        #expect(Format.tokens(470_000) == "470k")
        #expect(Format.tokens(1_000_000) == "1M")
        #expect(Format.tokens(1_250_000) == "1.25M")
        #expect(Format.tokens(950) == "950")
        #expect(Format.tokens(64_499) == "64k")
    }

    @Test func model() {
        #expect(Format.model("claude-opus-5-5") == "opus-5.5")
        #expect(Format.model("claude-haiku-4-5-20251001") == "haiku-4.5")
        #expect(Format.model("claude-sonnet-4") == "sonnet-4")
        #expect(Format.model("gpt-6.2-sol") == "gpt-6.2-sol")
    }

    @Test func numbers() {
        #expect(Format.percent(-0.2258) == "−23%")
        #expect(Format.percent(0.004) == "+0.4%")
        #expect(Format.usd(-2.1514) == "−$2.15")
        #expect(Format.duration(47 * 60) == "47 min")
        #expect(Format.duration(30) == "30 s")
        #expect(Format.ago(100, now: Date(timeIntervalSince1970: 130)) == "just now")
    }
}

struct PanelTests {
    func snapshot() throws -> SessionsSnapshot {
        guard case .sessions(let s) = try EngineAPI.decode(Fixture.data("sessions")) else { throw CancellationError() }
        return s
    }

    @Test func sessionRowsGroupSubagentsUnderTheirMain() throws {
        let s = try snapshot()
        let now = Date(timeIntervalSince1970: s.at)
        let rows = Panel.sessionRows(s, now: now, showProjects: true)
        #expect(rows.map(\.id) == ["codex/cx-1", "claude-code/22222222-bbbb", "claude-code/11111111-aaaa"])
        let shop = try #require(rows.last)
        #expect(shop.subagents == 1)
        #expect(shop.title == "shop")
        #expect(shop.model == "opus-5.5")
        #expect(shop.remaining == 650_000 - 339_000)
        guard case .warm(let coldIn) = shop.cache else {
            Issue.record("expected a warm cache")
            return
        }
        #expect(abs(coldIn - (1_795_431_024.842 - s.at)) < 0.01)
        let blog = rows[1]
        #expect(blog.ctx == nil && blog.compactedAgo != nil)
        #expect(rows[0].cache == .unknown)
    }

    @Test func coldCacheRewritesTheContext() throws {
        let s = try snapshot()
        let later = Date(timeIntervalSince1970: 1_795_431_024.842 + 1)
        let shop = try #require(Panel.sessionRows(s, now: later, showProjects: true).last)
        #expect(shop.cache == .cold)
        #expect(shop.coldRewrite == 339_000)
    }

    @Test func projectNamesCanBeHidden() throws {
        let rows = Panel.sessionRows(try snapshot(), now: Date(), showProjects: false)
        #expect(rows.map(\.title) == ["Codex", "Claude Code", "Claude Code"])
    }

    @Test func orphanSubagentGetsItsOwnRow() throws {
        var s = try snapshot()
        s.sessions.removeAll { $0.id == "11111111-aaaa" }
        let rows = Panel.sessionRows(s, now: Date(), showProjects: true)
        #expect(rows.contains { $0.id == "claude-code/agent-a1" && $0.title == "shop · subagent" })
    }

    @Test func icon() throws {
        let s = try snapshot()
        let now = Date(timeIntervalSince1970: s.at)
        #expect(Panel.icon(engineOK: false, sessions: s, now: now) == .engineProblem)
        #expect(Panel.icon(engineOK: true, sessions: nil, now: now) == .idle)
        // shop: 339k of 650k is the fullest session active in the last hour
        guard case .active(let f) = Panel.icon(engineOK: true, sessions: s, now: now) else {
            Issue.record("expected active")
            return
        }
        #expect(abs(f - 339_000.0 / 650_000.0) < 1e-9)
        var hot = s
        hot.sessions[0].ctx = 400_000  // cx-1 at 94% of 427.5k
        guard case .nearThreshold = Panel.icon(engineOK: true, sessions: hot, now: now) else {
            Issue.record("expected amber")
            return
        }
        #expect(Panel.icon(engineOK: true, sessions: s, now: now.addingTimeInterval(7200)) == .idle)
    }

    @Test func thresholdRows() throws {
        guard case .result(let r) = try EngineAPI.decode(Fixture.data("result")) else { throw CancellationError() }
        let rows = Panel.thresholdRows(r)
        // Codex's groups fold into its one key; main roles first.
        #expect(!rows.contains { $0.id.hasPrefix("codex/") })
        let codex = try #require(rows.first { $0.id == "global/codex" })
        #expect(codex.title == "Codex (all models)")
        #expect(codex.suggestsChange == false)
        let opus = try #require(rows.first)
        #expect(opus.id == "claude-code/claude-opus-5-5/main")
        #expect(opus.value == "625k")
        #expect(opus.suggestsChange)
        #expect(opus.detail.hasPrefix("set it (−23% cost)"))
        #expect(opus.detail.contains("only 5 days of data"))
        let firstSub = try #require(rows.firstIndex { $0.title.contains("subagent") })
        let lastMain = try #require(rows.lastIndex { !$0.title.contains("subagent") && $0.id.hasPrefix("claude-code/") })
        #expect(lastMain < firstSub)
    }

    @Test func codexKeepCurrentShowsTheSetting() {
        let key = GlobalKey(models: ["gpt-5-codex · main"], profiles: [:])
        let pick = GlobalKey.Pick(action: "keep current", threshold: 300_000, current: 320_000, dUsdPerDay: nil, dMinPerDay: nil)
        let row = Panel.globalRow(harness: "codex", key: key, pick: pick)
        #expect(row.value == "320k")
        #expect(row.detail == "keep your setting")
    }

    @Test func idleRow() {
        let keep = IdleVerdict.Choice(dUsdPerDay: 0.8, dMinPerDay: 0, shouldSwitch: false)
        let v = IdleVerdict(enabled: true, configured: false, minTokens: 200_000, profile: "balanced", profiles: ["balanced": keep])
        let row = Panel.idleRow(v, keep)
        #expect(row.value == "on")
        #expect(row.detail == "keep it on (turning it off: +$0.80/day)")
        let flip = IdleVerdict.Choice(dUsdPerDay: -1.5, dMinPerDay: 0, shouldSwitch: true)
        #expect(Panel.idleRow(v, flip).detail == "turn it off (−$1.50/day)")
    }
}

struct ScheduleTests {
    @Test func dueTimes() {
        #expect(Schedule.nextRun(service: ServiceState()) == .distantPast)
        let ok = ServiceState(failures: 0, lastAttempt: 1000, lastCalibrated: 1000)
        #expect(Schedule.nextRun(service: ok, every: 100).timeIntervalSince1970 == 1100)
        let failed = ServiceState(failures: 3, lastAttempt: 5000, lastCalibrated: 1000)
        #expect(Schedule.nextRun(service: failed, every: 86_400).timeIntervalSince1970 == 5000 + 1200)
        let capped = ServiceState(failures: 20, lastAttempt: 5000, lastCalibrated: nil)
        #expect(Schedule.nextRun(service: capped, every: 3600).timeIntervalSince1970 == 8600)
        #expect(Schedule.isDue(service: ok, every: 100, now: Date(timeIntervalSince1970: 1100)))
        #expect(!Schedule.isDue(service: ok, every: 100, now: Date(timeIntervalSince1970: 1099)))
    }

    @Test func power() {
        #expect(PowerState(lowPowerMode: true, onBattery: false, batteryPercent: nil).shouldDefer)
        #expect(PowerState(lowPowerMode: false, onBattery: true, batteryPercent: 19).shouldDefer)
        #expect(!PowerState(lowPowerMode: false, onBattery: true, batteryPercent: 20).shouldDefer)
        #expect(!PowerState(lowPowerMode: false, onBattery: false, batteryPercent: 5).shouldDefer)
    }
}

struct LocatorTests {
    @Test func order() throws {
        let tmp = try TempDir()
        let bin = tmp.url.appendingPathComponent("bin")
        try FileManager.default.createDirectory(at: bin, withIntermediateDirectories: true)
        let local = tmp.url.appendingPathComponent(".local/bin")
        try FileManager.default.createDirectory(at: local, withIntermediateDirectories: true)
        _ = try TempDirScript(dir: local)
        let env = ["PATH": bin.path]
        let loc = EngineLocator(customPath: nil, environment: env, home: tmp.url.path)
        #expect(loc.locate()?.path == local.appendingPathComponent("compactlab").path)
        // PATH wins over ~/.local/bin
        _ = try TempDirScript(dir: bin)
        #expect(loc.locate()?.path == bin.appendingPathComponent("compactlab").path)
        // a custom path wins over both; the environment override over everything
        let custom = try tmp.script("my-engine", "exit 0")
        #expect(EngineLocator(customPath: custom.path, environment: env, home: tmp.url.path).locate() == custom)
        let forced = try tmp.script("forced", "exit 0")
        let all = env.merging(["COMPACTLAB_ENGINE": forced.path]) { $1 }
        #expect(EngineLocator(customPath: custom.path, environment: all, home: tmp.url.path).locate() == forced)
        // a directory or a missing file is skipped
        #expect(EngineLocator(customPath: tmp.url.path, environment: [:], home: "/nonexistent").locate() == nil)
        #expect(loc.engineEnvironment["PATH"]?.hasPrefix(bin.path + ":" + local.path) == true)
    }
}

private struct TempDirScript {
    init(dir: URL) throws {
        let f = dir.appendingPathComponent("compactlab")
        try "#!/bin/sh\nexit 0\n".write(to: f, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: f.path)
    }
}
