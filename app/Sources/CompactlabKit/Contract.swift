import Foundation

/// The `compactlab engine` JSON contract (docs/ENGINE_API.md), version 1. Only the fields the app uses are decoded;
/// unknown fields and kinds are ignored, as the contract asks. Times are Unix seconds, token counts integers.
public enum EngineAPI {
    /// The contract version this app was built for. A different one: read on, write nothing.
    public static let version = 1

    static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    /// Decode one engine object (a stdout document or a `watch` line).
    public static func decode(_ data: Data) throws -> EngineMessage {
        let head = try decoder.decode(Header.self, from: data)
        switch head.kind {
        case "hello": return .hello(try decoder.decode(Hello.self, from: data))
        case "sessions": return .sessions(try decoder.decode(SessionsSnapshot.self, from: data))
        case "result": return .result(try decoder.decode(ResultSummary.self, from: data))
        case "error": return .error(try decoder.decode(ErrorEnvelope.self, from: data).error)
        case "pong": return .pong
        default: return .unknown(kind: head.kind)
        }
    }

    struct Header: Decodable {
        var engineApi: Int
        var kind: String
    }

    struct ErrorEnvelope: Decodable {
        var error: EngineError
    }
}

public enum EngineMessage: Sendable {
    case hello(Hello)
    case sessions(SessionsSnapshot)
    case result(ResultSummary)
    case error(EngineError)
    case pong
    case unknown(kind: String)
}

public struct EngineError: Decodable, Sendable, Equatable, Error {
    public var code: String
    public var message: String

    public init(code: String, message: String) {
        self.code = code
        self.message = message
    }
}

public struct PathInfo: Decodable, Sendable, Equatable {
    public var path: String
    public var exists: Bool

    /// `path` with a leading `~` expanded.
    public var expanded: String { (path as NSString).expandingTildeInPath }
}

public struct ServiceState: Decodable, Sendable, Equatable {
    public var failures: Int
    public var lastAttempt: Double?
    public var lastCalibrated: Double?
    public var lastError: String?

    public init(failures: Int = 0, lastAttempt: Double? = nil, lastCalibrated: Double? = nil, lastError: String? = nil) {
        self.failures = failures
        self.lastAttempt = lastAttempt
        self.lastCalibrated = lastCalibrated
        self.lastError = lastError
    }
}

public struct Hello: Decodable, Sendable, Equatable {
    public struct Harness: Decodable, Sendable, Equatable {
        public var name: String
        public var live: Bool
        public var roots: [PathInfo]
        public var config: PathInfo?
    }

    public var engineApi: Int
    public var version: String
    public var python: String?
    public var commands: [String]
    public var liveHarnesses: [String]
    public var harnesses: [Harness]
    public var stateDir: String
    public var result: PathInfo
    public var service: ServiceState

    /// Existing log directories of the harnesses `watch` reads: what the app watches with FSEvents.
    public var liveRoots: [String] {
        harnesses.filter { $0.live }.flatMap { $0.roots.filter(\.exists).map(\.expanded) }
    }
}

public struct SessionsSnapshot: Decodable, Sendable, Equatable {
    public struct IdleSetting: Decodable, Sendable, Equatable {
        public var enabled: Bool?
        public var minTokens: Int?
    }

    public var at: Double
    public var activeS: Double
    public var unreadable: Int
    public var sessions: [Session]
    public var idleCompaction: IdleSetting?
}

public struct Session: Decodable, Sendable, Equatable, Identifiable {
    public var harness: String
    public var id: String
    public var role: String
    public var parentId: String?
    public var project: String?
    public var model: String?
    public var platform: String?
    public var ctx: Int?
    public var compactedAt: Double?
    public var window: Int?
    public var windowSource: String?
    public var threshold: Int?
    public var thresholdSource: String?
    public var thresholdNote: String?
    public var lastRequestAt: Double?
    public var lastActivityAt: Double?
    public var cacheTtlS: Int?
    public var cacheExpiresAt: Double?

    /// Unique across harnesses.
    public var key: String { "\(harness)/\(id)" }
    public var isSubagent: Bool { role == "subagent" }
}

public struct ResultSummary: Decodable, Sendable, Equatable {
    public struct Calibration: Decodable, Sendable, Equatable {
        public var at: Double?
        public var tookS: Double?
        public var reason: String?
        public var everyS: Double?
    }

    public struct HarnessRun: Decodable, Sendable, Equatable {
        public var mode: String?
        public var dataThrough: Double?
        public var calibration: Calibration?
    }

    public var calibration: Calibration?
    public var mode: String?
    public var dataThrough: Double?
    public var demo: Bool
    public var profile: String
    public var harnesses: [String: HarnessRun]?
    public var groups: [Group]
    public var idleCompaction: IdleVerdict?
    public var globalKeys: [String: GlobalKey]?
    public var service: ServiceState?
}

public struct Impact: Decodable, Sendable, Equatable {
    public var costPct: Double?
    public var timePct: Double?
    public var usdPerDay: Double?
    public var minPerDay: Double?
    public var compactionsPerDay: Double?
}

public struct ProfilePick: Decodable, Sendable, Equatable {
    public var action: String
    public var threshold: Int?
    public var flags: [String]
    public var vsDefault: Impact?
    public var vsCurrent: Impact?
}

public struct Group: Decodable, Sendable, Equatable, Identifiable {
    public var harness: String
    public var model: String
    public var role: String
    public var selected: String
    public var defaultThreshold: Int?
    public var currentThreshold: Int?
    public var currentNote: String?
    public var effectiveMax: Int?
    public var days: Double
    public var activeDays: Double?
    public var nSessions: Int?
    public var nTurns: Int?
    public var noActionNeeded: Bool
    public var supersededBy: String?
    public var profiles: [String: ProfilePick]

    public var id: String { "\(harness)/\(model)/\(role)" }
    public var pick: ProfilePick? { profiles[selected] }
}

/// Claude Code's idle compaction: keep it as it is, or flip it.
public struct IdleVerdict: Decodable, Sendable, Equatable {
    public struct Choice: Decodable, Sendable, Equatable {
        public var dUsdPerDay: Double
        public var dMinPerDay: Double
        public var shouldSwitch: Bool

        enum CodingKeys: String, CodingKey {
            case dUsdPerDay, dMinPerDay
            case shouldSwitch = "switch"
        }
    }

    public var enabled: Bool
    public var configured: Bool
    public var minTokens: Int
    public var profile: String
    public var profiles: [String: Choice]
}

/// One threshold for every model of a harness (Codex).
public struct GlobalKey: Decodable, Sendable, Equatable {
    public struct Pick: Decodable, Sendable, Equatable {
        public var action: String
        public var threshold: Int?
        /// The configured value, when the action is `keep current` (the pick may differ within tolerance).
        public var current: Int?
        public var dUsdPerDay: Double?
        public var dMinPerDay: Double?
    }

    public var models: [String]
    public var profiles: [String: Pick]
}
