import Foundation
import Testing

@testable import CompactlabKit

/// The app decodes every example in `tests/contract/` (the files the Python tests check the engine against).
struct ContractTests {
    @Test func hello() throws {
        guard case .hello(let h) = try EngineAPI.decode(Fixture.data("hello")) else {
            Issue.record("not hello")
            return
        }
        #expect(h.engineApi == EngineAPI.version)
        #expect(h.commands.contains("watch"))
        #expect(h.liveHarnesses == ["claude-code", "codex"])
        #expect(h.liveRoots == [
            ("~/.claude/projects" as NSString).expandingTildeInPath, ("~/.codex/sessions" as NSString).expandingTildeInPath,
        ])
        #expect(h.service.failures == 0)
        #expect(h.result.exists == false)
    }

    @Test func sessions() throws {
        guard case .sessions(let s) = try EngineAPI.decode(Fixture.data("sessions")) else {
            Issue.record("not sessions")
            return
        }
        #expect(s.sessions.count == 4)
        let sub = try #require(s.sessions.first { $0.id == "agent-a1" })
        #expect(sub.isSubagent && sub.parentId == "11111111-aaaa")
        #expect(sub.cacheTtlS == 300)
        let compacted = try #require(s.sessions.first { $0.id == "22222222-bbbb" })
        #expect(compacted.ctx == nil && compacted.compactedAt != nil)
        #expect(s.idleCompaction?.minTokens == 200_000)
    }

    @Test func result() throws {
        guard case .result(let r) = try EngineAPI.decode(Fixture.data("result")) else {
            Issue.record("not result")
            return
        }
        #expect(r.profile == "balanced")
        #expect(r.groups.count == 5)
        let g = r.groups[0]
        #expect(g.pick?.action == "set")
        #expect(g.pick?.threshold == 625_000)
        #expect(g.pick?.vsDefault?.costPct != nil)
        #expect(r.globalKeys?["codex"]?.profiles["cost"]?.threshold == 200_000)
        #expect(r.harnesses?["codex"]?.mode == "lite")
        #expect(r.calibration?.everyS == 86_400)
    }

    @Test func errorAndPong() throws {
        guard case .error(let e) = try EngineAPI.decode(Fixture.data("error")) else {
            Issue.record("not error")
            return
        }
        #expect(e.code == "no_result")
        guard case .pong = try EngineAPI.decode(Fixture.data("pong")) else {
            Issue.record("not pong")
            return
        }
    }

    @Test func unknownKindsAndFieldsAreIgnored() throws {
        let d = Data(#"{"engine_api": 1, "kind": "future", "x": [1]}"#.utf8)
        guard case .unknown(let k) = try EngineAPI.decode(d) else {
            Issue.record("expected unknown")
            return
        }
        #expect(k == "future")
        let p = Data(#"{"engine_api": 1, "kind": "pong", "new_field": {"a": 1}}"#.utf8)
        guard case .pong = try EngineAPI.decode(p) else {
            Issue.record("expected pong")
            return
        }
    }

    @Test func garbageThrows() {
        #expect(throws: (any Error).self) { try EngineAPI.decode(Data("Traceback (most recent call last):".utf8)) }
    }

    @Test func idleVerdict() throws {
        let json = #"""
            {"enabled": true, "configured": false, "min_tokens": 200000, "groups": ["claude-opus-5-5 · main"],
             "profile": "balanced", "current": {"d_usd_per_day": 1.0, "d_min_per_day": 0, "switch": false},
             "profiles": {"balanced": {"d_usd_per_day": -2.5, "d_min_per_day": 1.2, "d_score_pct": -0.04,
                                        "d_recall": 0, "d_compactions_per_day": 1, "switch": true,
                                        "configured": {}, "flipped": {}}}}
            """#
        let v = try EngineAPI.decoder.decode(IdleVerdict.self, from: Data(json.utf8))
        #expect(v.enabled && v.profiles["balanced"]?.shouldSwitch == true)
    }
}
