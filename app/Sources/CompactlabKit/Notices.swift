import Foundation

/// Localized from the app bundle (`Contents/Resources/<language>.lproj/Localizable.strings`); English, the keys
/// themselves, when there is no translation (tests, `swift run`). Interpolate only strings, so every key's
/// placeholders are `%@`.
public func L(_ key: String.LocalizationValue) -> String {
    String(localized: key, bundle: .main)
}

/// A notification the app should post.
public struct Notice: Equatable, Sendable {
    public enum Kind: String, Sendable {
        /// Two calibrations in a row agree on a change worth making.
        case suggestion
        /// A setting compactlab wrote was changed by someone else.
        case drift
        /// The verification of the latest change has a conclusion.
        case verification
        /// Auto-apply changed a setting.
        case autoApplied
        /// Calibration failed several times in a row.
        case problem
    }

    public enum Action: Equatable, Sendable {
        case none
        case review
        /// Undo this apply (its manifest id).
        case undo(String)
    }

    /// Stable: the same notice is never posted twice.
    public var id: String
    public var kind: Kind
    public var title: String
    public var body: String
    public var action: Action
}

/// What has been noticed so far (kept in UserDefaults between launches).
public struct NoticeState: Codable, Equatable, Sendable {
    /// The suggestions of the latest calibration and of the one before: key → direction (+1 up, −1 down).
    public var calibrationAt: Double?
    public var suggestions: [String: Int] = [:]
    public var previous: [String: Int] = [:]
    /// Notice ids already posted.
    public var sent: Set<String> = []
    /// Suggestion ids ("Remind me in a week") → until when.
    public var snoozed: [String: Double] = [:]

    public init() {}
}

public enum Notices {
    /// Calibration failures in a row before saying so.
    public static let failuresToReport = 3
    public static let snooze: TimeInterval = 7 * 86_400
    /// A suggestion worth a notification: at least this much cheaper per day...
    public static let minCostShare = 0.02
    /// ...or this many minutes a day.
    public static let minMinutes = 2.0

    public struct Enabled: Sendable {
        public var suggestions: Bool
        public var changes: Bool
        public var problems: Bool

        public init(suggestions: Bool = true, changes: Bool = true, problems: Bool = true) {
            self.suggestions = suggestions
            self.changes = changes
            self.problems = problems
        }
    }

    public static func suggestionId(_ c: Plan.Change) -> String { "suggest:\(c.key)=\(c.new)" }

    /// +1 when the change raises the threshold in force, −1 when it lowers it (0 when that is unknown).
    static func direction(_ c: Plan.Change, result: ResultSummary?) -> Int {
        let base =
            c.old.tokens
            ?? result?.groups.first { $0.harness == c.harness && $0.role == "main" && matches($0.model, c.model) }?
            .defaultThreshold
        guard let base else { return 0 }
        return c.new > base ? 1 : (c.new < base ? -1 : 0)
    }

    static func matches(_ groupModel: String, _ key: String) -> Bool {
        groupModel == key || groupModel.hasPrefix(key + "-") || key.hasPrefix(groupModel)
    }

    static func worthIt(_ i: Plan.Change.Impact) -> Bool {
        if let p = i.costPct, -p >= minCostShare { return true }
        if let m = i.minPerDay, -m >= minMinutes { return true }
        return i.costPct == nil && (i.usdPerDay ?? 0) < 0  // one key for every model: no share; any saving counts
    }

    /// Changes of `plan` the last two calibrations agree on and that are worth the bother, minus snoozed ones.
    public static func stable(_ plan: Plan?, result: ResultSummary?, state: NoticeState, now: Date) -> [Plan.Change] {
        (plan?.changes ?? []).filter { c in
            guard worthIt(c.impact), let before = state.previous[c.key] else { return false }
            guard before == direction(c, result: result) else { return false }
            return (state.snoozed[suggestionId(c)] ?? 0) < now.timeIntervalSince1970
        }
    }

    /// Something waits for the person: a stable suggestion, or a setting changed behind compactlab's back.
    public static func pending(plan: Plan?, result: ResultSummary?, history: History?, state: NoticeState, now: Date)
        -> Bool
    {
        !stable(plan, result: result, state: state, now: now).isEmpty || !(history?.applies.first?.drifted ?? []).isEmpty
    }

    /// Record the plan of the current result: a new calibration moves the latest suggestions to `previous`.
    public static func observe(_ plan: Plan?, result: ResultSummary?, state: inout NoticeState) {
        guard let at = result?.calibration?.at else { return }
        let now = Dictionary(
            (plan?.changes ?? []).map { ($0.key, direction($0, result: result)) }, uniquingKeysWith: { a, _ in a })
        if state.calibrationAt != at {
            if state.calibrationAt != nil { state.previous = state.suggestions }
            state.calibrationAt = at
        }
        state.suggestions = now
    }

    /// The notices to post now, each once; `state` records them.
    public static func evaluate(
        plan: Plan?, result: ResultSummary?, history: History?, verification: Verification?, service: ServiceState?,
        enabled: Enabled, state: inout NoticeState, now: Date
    ) -> [Notice] {
        observe(plan, result: result, state: &state)
        var out: [Notice] = []
        func post(_ n: Notice, if on: Bool) {
            guard !state.sent.contains(n.id) else { return }
            state.sent.insert(n.id)  // a notice turned off is not posted later either
            if on { out.append(n) }
        }

        let stable = stable(plan, result: result, state: state, now: now)
        if !stable.isEmpty {
            let id = stable.map(suggestionId).sorted().joined(separator: ",")
            post(suggestionNotice(stable, id: id), if: enabled.suggestions)
        }
        if let m = result?.autoApply?.manifestId {
            let changed = history?.applies.first { $0.id == m }?.changes ?? []
            post(
                Notice(
                    id: "auto:\(m)", kind: .autoApplied, title: L("compactlab changed a setting"),
                    body: changed.isEmpty ? L("Auto-apply wrote a new threshold.") : changed.map(Panel.changeLine).joined(separator: "\n"),
                    action: .undo(m)),
                if: enabled.changes)
        }
        if let newest = history?.applies.first {
            for row in newest.drifted {
                let nowText = row.now.tokens.map(Format.tokens) ?? L("removed")
                post(
                    Notice(
                        id: "drift:\(newest.id):\(row.key):\(nowText)", kind: .drift, title: L("A setting was changed"),
                        body: L("\(Panel.changeTitle(harness: row.harness, model: row.model)): compactlab set \(Format.tokens(row.new)), now \(nowText). compactlab will leave it as it is."),
                        action: .none),
                    if: enabled.changes)
            }
            if let v = verification, v.id == newest.id, v.isFinal {
                post(
                    Notice(
                        id: "verify:\(v.id)", kind: .verification, title: L("The last change is verified"),
                        body: v.groups.map { "\(Panel.changeTitle(harness: $0.harness, model: $0.model)): \(Panel.verdict($0))" }
                            .joined(separator: "\n"),
                        action: .none),
                    if: enabled.changes)
            }
        }
        if let s = service {
            if s.failures >= failuresToReport {
                post(
                    Notice(
                        id: "failing:\(Int(s.lastAttempt ?? 0) / 86_400)", kind: .problem,
                        title: L("Calibration keeps failing"),
                        body: s.lastError ?? L("\(String(s.failures)) failures in a row."), action: .none),
                    if: enabled.problems)
            }
        }
        return out
    }

    static func suggestionNotice(_ changes: [Plan.Change], id: String) -> Notice {
        let body: String
        if changes.count == 1, let c = changes.first {
            body = Panel.planLine(c)
        } else {
            let usd = changes.compactMap(\.impact.usdPerDay).reduce(0, +)
            body = L("\(String(changes.count)) changes, \(Format.usd(usd))/day in all.")
        }
        return Notice(
            id: id, kind: .suggestion, title: L("compactlab suggests a change"),
            body: body + "\n" + L("The last two calibrations agree."), action: .review)
    }
}
