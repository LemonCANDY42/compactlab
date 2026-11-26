import Foundation

/// Text and numbers the panel shows, kept apart from the views so they can be tested.
public enum Format {
    /// 470000 → "470k", 1000000 → "1M", 1250000 → "1.25M", 950 → "950".
    public static func tokens(_ n: Int) -> String {
        let a = abs(n)
        if a >= 1_000_000 {
            let m = Double(n) / 1_000_000
            return trimmed(m, digits: 2) + "M"
        }
        if a >= 1_000 { return "\(Int((Double(n) / 1_000).rounded()))k" }
        return "\(n)"
    }

    /// "claude-opus-5-5" → "opus-5.5", "claude-haiku-4-5-20251001" → "haiku-4.5"; other ids as they are.
    public static func model(_ id: String) -> String {
        var s = id
        if let r = s.range(of: #"-\d{8}$"#, options: .regularExpression) { s.removeSubrange(r) }
        guard s.hasPrefix("claude-") else { return s }
        s.removeFirst("claude-".count)
        if let r = s.range(of: #"-(\d+)-(\d+)$"#, options: .regularExpression) {
            let parts = s[r].dropFirst().split(separator: "-")
            s.replaceSubrange(r, with: "-\(parts[0]).\(parts[1])")
        }
        return s
    }

    /// A span of seconds as "45 s", "12 min", "3 h", "2 d".
    public static func duration(_ seconds: TimeInterval) -> String {
        let s = max(0, seconds)
        if s < 60 { return L("\(String(Int(s))) s") }
        if s < 3600 { return L("\(String(Int(s / 60))) min") }
        if s < 86_400 * 2 { return L("\(String(Int(s / 3600))) h") }
        return L("\(String(Int(s / 86_400))) d")
    }

    /// "just now", "12 min ago", "3 h ago".
    public static func ago(_ t: Double, now: Date) -> String {
        let s = now.timeIntervalSince1970 - t
        return s < 60 ? L("just now") : L("\(duration(s)) ago")
    }

    /// −0.21 → "−21%", 0.004 → "+0.4%".
    public static func percent(_ x: Double) -> String {
        let p = x * 100
        let body = abs(p) < 1 && p != 0 ? trimmed(abs(p), digits: 1) : "\(Int(abs(p).rounded()))"
        return (p < 0 ? "−" : "+") + body + "%"
    }

    /// A profile's name in the interface language.
    public static func profile(_ name: String) -> String {
        switch name {
        case "cost": L("cost")
        case "balanced": L("balanced")
        case "time": L("time")
        default: name
        }
    }

    /// 0.12 → "+0.1", −2.6 → "−2.6".
    public static func signed(_ x: Double, digits: Int) -> String {
        (x < 0 ? "−" : "+") + String(format: "%.\(digits)f", abs(x))
    }

    /// −2.15 → "−$2.15", 11.8 → "+$11.80".
    public static func usd(_ x: Double) -> String {
        (x < 0 ? "−" : "+") + "$" + String(format: "%.2f", abs(x))
    }

    static func trimmed(_ x: Double, digits: Int) -> String {
        var s = String(format: "%.\(digits)f", x)
        while s.contains("."), s.hasSuffix("0") { s.removeLast() }
        if s.hasSuffix(".") { s.removeLast() }
        return s
    }
}

/// One row of the active-sessions list.
public struct SessionRow: Identifiable, Sendable, Equatable {
    public enum Cache: Sendable, Equatable {
        case unknown
        case warm(coldIn: TimeInterval)
        case cold
    }

    public var id: String
    public var title: String
    public var model: String
    public var ctx: Int?
    public var threshold: Int?
    /// ctx / threshold, 0...1+.
    public var fraction: Double?
    public var compactedAgo: TimeInterval?
    public var cache: Cache
    public var subagents: Int
    public var lastActivity: Double?
    public var harness: String

    /// Tokens until auto-compaction.
    public var remaining: Int? {
        guard let ctx, let threshold else { return nil }
        return max(0, threshold - ctx)
    }

    /// What a send would write to cache now that the cache has gone cold: the whole context again.
    public var coldRewrite: Int? {
        guard case .cold = cache else { return nil }
        return ctx
    }
}

/// What the menu bar icon shows.
public enum IconState: Sendable, Equatable {
    case engineProblem
    case idle
    case active(fraction: Double)
    /// A recently active session is past `Panel.warnAt` of its threshold.
    case nearThreshold(fraction: Double)

    public var fraction: Double? {
        switch self {
        case .active(let f), .nearThreshold(let f): f
        default: nil
        }
    }
}

/// One row of the threshold table.
public struct ThresholdRow: Identifiable, Sendable, Equatable {
    public var id: String
    public var title: String
    public var value: String
    public var detail: String
    /// The pick would change a setting.
    public var suggestsChange: Bool
}

public enum Panel {
    /// Amber icon past this share of the threshold.
    public static let warnAt = 0.85
    /// Sessions written to within this long count for the icon.
    public static let iconRecency: TimeInterval = 3600
    /// Fewer days of data than this: say so beside the pick.
    public static let fewDays = 7.0

    /// Main sessions newest first, each with its subagents counted under it (subagents of a main session not in the
    /// list get rows of their own).
    public static func sessionRows(_ snap: SessionsSnapshot, now: Date, showProjects: Bool) -> [SessionRow] {
        let mains = Set(snap.sessions.filter { !$0.isSubagent }.map { "\($0.harness)/\($0.id)" })
        var subCount: [String: Int] = [:]
        for s in snap.sessions where s.isSubagent {
            if let p = s.parentId, mains.contains("\(s.harness)/\(p)") { subCount["\(s.harness)/\(p)", default: 0] += 1 }
        }
        let shown = snap.sessions.filter { s in
            !s.isSubagent || !(s.parentId.map { mains.contains("\(s.harness)/\($0)") } ?? false)
        }
        return shown
            .sorted { ($0.lastActivityAt ?? 0) > ($1.lastActivityAt ?? 0) }
            .map { row($0, subagents: subCount[$0.key] ?? 0, now: now, showProjects: showProjects) }
    }

    static func row(_ s: Session, subagents: Int, now: Date, showProjects: Bool) -> SessionRow {
        let t = now.timeIntervalSince1970
        let cache: SessionRow.Cache =
            if let exp = s.cacheExpiresAt { exp > t ? .warm(coldIn: exp - t) : .cold } else { .unknown }
        var fraction: Double?
        if let c = s.ctx, let th = s.threshold, th > 0 { fraction = Double(c) / Double(th) }
        let project = showProjects ? s.project : nil
        let name = project ?? (s.harness == "codex" ? "Codex" : "Claude Code")
        return SessionRow(
            id: s.key,
            title: s.isSubagent ? L("\(name) · subagent") : name,
            model: s.model.map(Format.model) ?? L("unknown model"),
            ctx: s.ctx,
            threshold: s.threshold,
            fraction: fraction,
            compactedAgo: s.ctx == nil ? s.compactedAt.map { t - $0 } : nil,
            cache: cache,
            subagents: subagents,
            lastActivity: s.lastActivityAt,
            harness: s.harness)
    }

    public static func icon(engineOK: Bool, sessions: SessionsSnapshot?, now: Date) -> IconState {
        guard engineOK else { return .engineProblem }
        let t = now.timeIntervalSince1970
        let recent = (sessions?.sessions ?? []).filter { t - ($0.lastActivityAt ?? 0) < iconRecency }
        guard !recent.isEmpty else { return .idle }
        let top = recent.compactMap { s -> Double? in
            guard let c = s.ctx, let th = s.threshold, th > 0 else { return nil }
            return Double(c) / Double(th)
        }.max() ?? 0
        return top >= warnAt ? .nearThreshold(fraction: top) : .active(fraction: top)
    }

    /// One row per group, except harnesses with one key for all models (Codex), which get one row for that key, and
    /// a last row for Claude Code's idle compaction. Main sessions before subagents.
    public static func thresholdRows(_ r: ResultSummary) -> [ThresholdRow] {
        let global = r.globalKeys ?? [:]
        var rows: [ThresholdRow] = []
        let groups = r.groups
            .filter { global[$0.harness] == nil && $0.supersededBy == nil }
            .sorted(by: groupOrder)
        for g in groups { rows.append(groupRow(g)) }
        for (h, key) in global.sorted(by: { $0.key < $1.key }) {
            guard let p = key.profiles[r.profile] else { continue }
            rows.append(globalRow(harness: h, key: key, pick: p))
        }
        if let idle = r.idleCompaction, let c = idle.profiles[r.profile] ?? idle.profiles[idle.profile] {
            rows.append(idleRow(idle, c))
        }
        return rows
    }

    static func groupOrder(_ a: Group, _ b: Group) -> Bool {
        let ra = a.role == "main" ? 0 : 1, rb = b.role == "main" ? 0 : 1
        if ra != rb { return ra < rb }
        if a.harness != b.harness { return a.harness < b.harness }
        return a.model < b.model
    }

    static func groupRow(_ g: Group) -> ThresholdRow {
        let title = Format.model(g.model) + (g.role == "main" ? "" : " · " + (g.role == "subagent" ? L("subagent") : g.role))
        guard let p = g.pick else {
            return ThresholdRow(id: g.id, title: title, value: "–", detail: L("no pick"), suggestsChange: false)
        }
        let inForce = g.currentThreshold ?? g.defaultThreshold
        var detail: String
        var change = false
        if g.noActionNeeded || p.action == "no action needed" {
            detail = L("no change needed: sessions never reach the threshold")
        } else {
            switch p.action {
            case "set":
                change = true
                let vs = p.vsCurrent ?? p.vsDefault
                detail = vs?.costPct.map { L("set it (\(Format.percent($0)) cost)") } ?? L("set it")
            case "keep current": detail = L("keep your setting")
            case "keep default": detail = L("keep the default")
            default: detail = p.action
            }
        }
        if g.days < fewDays { detail += " · " + L("only \(String(Int(g.days.rounded(.up)))) days of data") }
        let value = (change ? p.threshold : (inForce ?? p.threshold)).map(Format.tokens) ?? "–"
        return ThresholdRow(id: g.id, title: title, value: value, detail: detail, suggestsChange: change)
    }

    static func globalRow(harness: String, key: GlobalKey, pick: GlobalKey.Pick) -> ThresholdRow {
        let name = harness == "codex" ? "Codex" : harness
        let change = pick.action == "set"
        var detail =
            change ? L("set it (one key for every model)") : (pick.action == "keep current" ? L("keep your setting") : L("keep the default"))
        if change, let usd = pick.dUsdPerDay { detail += ", " + L("\(Format.usd(usd))/day") }
        let shown = pick.action == "keep current" ? (pick.current ?? pick.threshold) : pick.threshold
        return ThresholdRow(
            id: "global/\(harness)", title: L("\(name) (all models)"), value: shown.map(Format.tokens) ?? "–",
            detail: detail, suggestsChange: change)
    }

    static func idleRow(_ idle: IdleVerdict, _ c: IdleVerdict.Choice) -> ThresholdRow {
        let state = idle.enabled ? L("on") : L("off")
        let usd = Format.usd(c.dUsdPerDay)
        let detail: String
        switch (c.shouldSwitch, idle.enabled) {
        case (true, true): detail = L("turn it off (\(usd)/day)")
        case (true, false): detail = L("turn it on (\(usd)/day)")
        case (false, true): detail = L("keep it on (turning it off: \(usd)/day)")
        case (false, false): detail = L("keep it off (turning it on: \(usd)/day)")
        }
        return ThresholdRow(id: "idle", title: L("Idle compaction"), value: state, detail: detail, suggestsChange: c.shouldSwitch)
    }
}

// MARK: - Changing settings

extension Panel {
    /// "opus-5.5", or "Codex" for its one key.
    public static func changeTitle(harness: String, model: String) -> String {
        harness == "codex" ? L("Codex (all models)") : Format.model(model)
    }

    /// "default" or "650k".
    public static func setting(_ v: SettingValue) -> String {
        switch v {
        case .unset: L("default")
        case .tokens(let n): Format.tokens(n)
        case .other: L("other")
        }
    }

    /// "opus-5.5: default → 625k, −$11.85/day"
    public static func planLine(_ c: Plan.Change) -> String {
        var s = "\(changeTitle(harness: c.harness, model: c.model)): \(setting(c.old)) → \(Format.tokens(c.new))"
        if let usd = c.impact.usdPerDay { s += ", " + L("\(Format.usd(usd))/day") }
        return s
    }

    /// "opus-5.5: 650k → 550k"
    public static func changeLine(_ r: History.Row) -> String {
        "\(changeTitle(harness: r.harness, model: r.model)): \(setting(r.old)) → \(Format.tokens(r.new))"
    }

    /// What a change does per day: "−$11.85/day (−23% cost), +0.1 min/day, against the default".
    public static func impact(_ i: Plan.Change.Impact) -> String {
        var s = ""
        if let usd = i.usdPerDay {
            s = i.costPct.map { L("\(Format.usd(usd))/day (\(Format.percent($0)) cost)") } ?? L("\(Format.usd(usd))/day")
        }
        if let m = i.minPerDay {
            let min = Format.signed(m, digits: 1)
            s = s.isEmpty ? L("\(min) min/day") : L("\(s), \(min) min/day")
        }
        switch i.against {
        case "current": s += " " + L("against your setting")
        case "default": s += " " + L("against the default")
        default: break
        }
        return s
    }

    /// "4 days, 3 sessions of data"
    public static func evidence(_ i: Plan.Change.Impact) -> String? {
        guard let d = i.days else { return nil }
        let days = String(Int(d.rounded(.up)))
        return i.sessions.map { L("\(days) days, \(String($0)) sessions of data") } ?? L("\(days) days of data")
    }

    /// verify's verdict in a few words.
    public static func verdict(_ g: Verification.Group) -> String {
        let observed = g.costPct.map { Format.percent($0) }
        let predicted = g.costPctPredicted.map { Format.percent($0) }
        switch g.verdict {
        case "consistent":
            return L("as predicted") + (observed.map { " (" + L("cost \($0)") + ")" } ?? "")
        case "less":
            return L("saved less than predicted") + pair(observed, predicted)
        case "more":
            return L("saved more than predicted") + pair(observed, predicted)
        case "untouched": return L("no effect: sessions did not reach the threshold")
        case "already": return L("it was in effect before the recorded time")
        case "unclear": return L("not enough turns to compare")
        case "too_early": return L("too early to tell")
        case "no_data": return L("no sessions since the change")
        default: return g.verdict
        }
    }

    static func pair(_ observed: String?, _ predicted: String?) -> String {
        guard let o = observed, let p = predicted else { return "" }
        return " (" + L("cost \(o), predicted \(p)") + ")"
    }

    /// "in effect", "undone", "changed since (now 600k)".
    public static func status(_ r: History.Row) -> String {
        switch r.status {
        case "in_effect": L("in effect")
        case "reverted": L("undone")
        default: L("changed since (now \(setting(r.now)))")
        }
    }
}
