import Foundation
import Testing

@testable import CompactlabKit

struct SettingsContractTests {
    @Test func decodesTheSettingsKinds() throws {
        guard case .plan(let p) = try EngineAPI.decode(Fixture.data("plan")) else { Issue.record("plan"); return }
        #expect(p.changes.count == 2 && p.planHash.count == 16)
        let c = try #require(p.changes.first)
        #expect(c.old == .unset && c.new == 625_000 && c.selector == "claude-code:claude-opus-5-5")
        #expect(c.impact.against == "default" && (c.impact.usdPerDay ?? 0) < 0 && c.impact.sessions == 3)
        guard case .applied(let a) = try EngineAPI.decode(Fixture.data("applied")) else { Issue.record("applied"); return }
        #expect(a.changes.count == 2)
        guard case .reverted(let r) = try EngineAPI.decode(Fixture.data("reverted")) else { Issue.record("reverted"); return }
        #expect(r.complete)
        guard case .history(let h) = try EngineAPI.decode(Fixture.data("history")) else { Issue.record("history"); return }
        #expect(h.applies.first?.inEffect == true && h.applies.first?.drifted.isEmpty == true)
        guard case .verification(let v) = try EngineAPI.decode(Fixture.data("verification")) else {
            Issue.record("verification")
            return
        }
        #expect(!v.isFinal && v.groups.allSatisfy { $0.verdict == "no_data" })
        guard case .consent(let con) = try EngineAPI.decode(Fixture.data("consent")) else { Issue.record("consent"); return }
        #expect(con?.models == ["*"] && con?.harness == "claude-code")
        guard case .hello(let hello) = try EngineAPI.decode(Fixture.data("hello")) else { Issue.record("hello"); return }
        #expect(hello.canChangeSettings && hello.consent == nil)
    }

    @Test func settingValues() throws {
        let rows = try JSONDecoder().decode([SettingValue].self, from: Data(#"[null, 650000, "unreadable"]"#.utf8))
        #expect(rows == [.unset, .tokens(650_000), .other])
        #expect(Panel.setting(.unset) == "default" && Panel.setting(.tokens(650_000)) == "650k")
    }
}

struct NoticesTests {
    func fixtures() throws -> (Plan, ResultSummary, History) {
        guard case .plan(let p) = try EngineAPI.decode(Fixture.data("plan")),
            case .result(let r) = try EngineAPI.decode(Fixture.data("result")),
            case .history(let h) = try EngineAPI.decode(Fixture.data("history"))
        else { throw EngineError(code: "fixture", message: "") }
        return (p, r, h)
    }

    func calibrated(_ r: ResultSummary, at: Double) -> ResultSummary {
        var r = r
        r.calibration?.at = at
        return r
    }

    @Test func aSuggestionNeedsTwoCalibrationsThatAgree() throws {
        let (plan, r, _) = try fixtures()
        var state = NoticeState()
        let now = Date()
        var n = Notices.evaluate(plan: plan, result: calibrated(r, at: 1), history: nil, verification: nil, service: nil,
                                 enabled: .init(), state: &state, now: now)
        #expect(n.isEmpty)  // one calibration says so
        n = Notices.evaluate(plan: plan, result: calibrated(r, at: 1), history: nil, verification: nil, service: nil,
                             enabled: .init(), state: &state, now: now)
        #expect(n.isEmpty)  // the same calibration again (a profile switch, a reload) does not count twice
        n = Notices.evaluate(plan: plan, result: calibrated(r, at: 2), history: nil, verification: nil, service: nil,
                             enabled: .init(), state: &state, now: now)
        #expect(n.map(\.kind) == [.suggestion] && n.first?.action == .review)
        #expect(Notices.pending(plan: plan, result: r, history: nil, state: state, now: now))
        n = Notices.evaluate(plan: plan, result: calibrated(r, at: 3), history: nil, verification: nil, service: nil,
                             enabled: .init(), state: &state, now: now)
        #expect(n.isEmpty)  // posted once
    }

    @Test func snoozedSuggestionsWaitAWeek() throws {
        let (plan, r, _) = try fixtures()
        var state = NoticeState()
        let now = Date()
        _ = Notices.evaluate(plan: plan, result: calibrated(r, at: 1), history: nil, verification: nil, service: nil,
                             enabled: .init(), state: &state, now: now)
        for c in plan.changes { state.snoozed[Notices.suggestionId(c)] = now.timeIntervalSince1970 + 600 }
        let n = Notices.evaluate(plan: plan, result: calibrated(r, at: 2), history: nil, verification: nil, service: nil,
                                 enabled: .init(), state: &state, now: now)
        #expect(n.isEmpty && !Notices.pending(plan: plan, result: r, history: nil, state: state, now: now))
        #expect(Notices.stable(plan, result: r, state: state, now: now.addingTimeInterval(700)).count == 2)
    }

    @Test func aTurnedOffKindIsNotPostedLaterEither() throws {
        let (plan, r, _) = try fixtures()
        var state = NoticeState()
        let off = Notices.Enabled(suggestions: false)
        for at in [1.0, 2.0] {
            _ = Notices.evaluate(plan: plan, result: calibrated(r, at: at), history: nil, verification: nil, service: nil,
                                 enabled: off, state: &state, now: Date())
        }
        let n = Notices.evaluate(plan: plan, result: calibrated(r, at: 2), history: nil, verification: nil, service: nil,
                                 enabled: .init(), state: &state, now: Date())
        #expect(n.isEmpty)
    }

    @Test func driftVerificationAutoApplyAndFailures() throws {
        let (_, r, h) = try fixtures()
        var history = h
        history.applies[0].changes[0].status = "changed"
        history.applies[0].changes[0].now = .tokens(300_000)
        var auto = r
        auto.autoApply = AutoApply(decisions: nil, manifest: "/x/manifests/\(h.applies[0].id).json", error: nil)
        let v = Verification(
            id: h.applies[0].id, appliedAt: 0,
            groups: [.init(harness: "claude-code", model: "claude-opus-5-5", role: "main", verdict: "consistent",
                           summary: nil, inEffect: "yes", costPct: -0.2, costPctPredicted: -0.22, sessionsBefore: 9,
                           sessionsAfter: 8)])  // fmt: skip
        var state = NoticeState()
        let failing = ServiceState(failures: 3, lastAttempt: 100, lastCalibrated: nil, lastError: "no sessions")
        let n = Notices.evaluate(plan: nil, result: auto, history: history, verification: v, service: failing,
                                 enabled: .init(), state: &state, now: Date())
        #expect(n.map(\.kind) == [.autoApplied, .drift, .verification, .problem])
        #expect(n[0].action == .undo(h.applies[0].id))
        #expect(n[1].body.contains("now 300k") && n[2].body.contains("as predicted") && n[3].body == "no sessions")
        #expect(Notices.pending(plan: nil, result: r, history: history, state: state, now: Date()))
        let again = Notices.evaluate(plan: nil, result: auto, history: history, verification: v, service: failing,
                                     enabled: .init(), state: &state, now: Date())
        #expect(again.isEmpty)
    }
}

extension AppModelTests {
    @Test func reviewApplyAndUndo() async throws {
        let engine = try FakeEngine()
        let m = model(engine)
        var posted: [Notice] = []
        m.post = { posted.append($0) }
        await m.start()
        defer { m.shutdown() }
        #expect(m.canChangeSettings && m.plan?.changes.count == 2 && m.history?.applies.isEmpty == true)

        let one = try #require(m.plan?.changes.first)
        await m.apply(one)
        #expect(engine.calls.contains("engine apply --only \(one.selector) --expect-plan \(one.planHash)"))
        #expect(m.writeMessage == nil && m.history?.applies.first?.inEffect == true)

        await m.apply(nil)
        #expect(engine.calls.contains("engine apply --expect-plan \(m.plan!.planHash)"))

        let id = try #require(m.history?.applies.first?.id)
        await m.revert(id)
        #expect(engine.calls.contains("engine revert --id \(id)"))
        #expect(posted.isEmpty)  // the person's own clicks are not notified
    }

    @Test func aChangedPlanIsNotWrittenAndSaysSo() async throws {
        let engine = try FakeEngine()
        try engine.set("plan-changed", true)
        let m = model(engine)
        await m.start()
        defer { m.shutdown() }
        await m.apply(nil)
        #expect(m.writeMessage?.contains("changed since") == true)
        #expect(m.history?.applies.isEmpty == true)
        #expect(engine.calls.filter { $0 == "engine plan" }.count >= 2)  // planned again to show what changed
    }

    @Test func autoApplyIsTheEnginesConsent() async throws {
        let engine = try FakeEngine()
        let m = model(engine)
        await m.start()
        defer { m.shutdown() }
        #expect(!m.autoApply)
        await m.setAutoApply(true)
        #expect(m.autoApply && engine.calls.contains { $0.hasPrefix("engine consent set") })
        m.recalibrate()
        #expect(await eventually { engine.calls.contains { $0.hasPrefix("engine calibrate") && $0.contains("--auto-apply") } })
        #expect(await eventually { !m.isCalibrating })
        await m.setAutoApply(false)
        #expect(!m.autoApply)
    }

    @Test func verifiesTheNewestChangeOnceADay() async throws {
        let engine = try FakeEngine()
        try engine.set("applied", true)
        // the apply three days ago; the verification is of it
        for (name, edit) in [
            ("history", { (d: inout [String: Any]) in
                var a = (d["applies"] as! [[String: Any]])
                a[0]["at"] = Date().timeIntervalSince1970 - 3 * 86_400
                d["applies"] = a
            }),
            ("verification", { (d: inout [String: Any]) in
                let h = try! JSONSerialization.jsonObject(with: Fixture.data("history")) as! [String: Any]
                d["id"] = (h["applies"] as! [[String: Any]])[0]["id"]
            }),
        ] {
            var d = try JSONSerialization.jsonObject(with: Fixture.data(name)) as! [String: Any]
            edit(&d)
            _ = try engine.tmp.write("\(name).json", String(decoding: try JSONSerialization.data(withJSONObject: d), as: UTF8.self) + "\n")
        }
        let m = model(engine)
        await m.start()
        defer { m.shutdown() }
        // the run is recorded, so a second call the same day does nothing
        await m.verifyIfDue()
        #expect(engine.calls.contains { $0.hasPrefix("engine verify --id \(m.history!.applies[0].id) --since-days ") })
        #expect(m.verification?.id == m.history?.applies.first?.id)
        let before = engine.calls.filter { $0.hasPrefix("engine verify") }.count
        await m.verifyIfDue()
        #expect(engine.calls.filter { $0.hasPrefix("engine verify") }.count == before)
    }
}
