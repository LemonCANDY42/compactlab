import Foundation

/// Finds the `compactlab` executable. An app started from Finder gets a minimal PATH, so the usual install places are
/// searched too. Order: `COMPACTLAB_ENGINE` (tests, CI), the path set in Settings, PATH, `~/.local/bin` (uv tool,
/// pipx), Homebrew, `/usr/local/bin`.
public struct EngineLocator: Sendable {
    public var customPath: String?
    public var environment: [String: String]
    public var home: String

    public init(
        customPath: String? = nil,
        environment: [String: String] = ProcessInfo.processInfo.environment,
        home: String = NSHomeDirectory()
    ) {
        self.customPath = customPath
        self.environment = environment
        self.home = home
    }

    /// Directories searched after the explicit paths; also prepended to the engine's own PATH.
    public var searchDirectories: [String] {
        let fromPath = (environment["PATH"] ?? "").split(separator: ":").map(String.init)
        let extra = ["\(home)/.local/bin", "/opt/homebrew/bin", "/usr/local/bin"]
        var seen = Set<String>()
        return (fromPath + extra).filter { !$0.isEmpty && seen.insert($0).inserted }
    }

    /// Every candidate in order, explicit ones first.
    public var candidates: [String] {
        var out: [String] = []
        if let e = environment["COMPACTLAB_ENGINE"], !e.isEmpty { out.append(e) }
        if let c = customPath?.trimmingCharacters(in: .whitespaces), !c.isEmpty {
            out.append((c as NSString).expandingTildeInPath)
        }
        out += searchDirectories.map { "\($0)/compactlab" }
        return out
    }

    public func locate() -> URL? {
        let fm = FileManager.default
        for path in candidates {
            var isDir: ObjCBool = false
            if fm.fileExists(atPath: path, isDirectory: &isDir), !isDir.boolValue, fm.isExecutableFile(atPath: path) {
                return URL(fileURLWithPath: path)
            }
        }
        return nil
    }

    /// The environment engine processes run with: this process's, with the search directories on PATH.
    public var engineEnvironment: [String: String] {
        var env = environment
        env["PATH"] = (searchDirectories + ["/usr/bin", "/bin", "/usr/sbin", "/sbin"])
            .reduce(into: [String]()) { if !$0.contains($1) { $0.append($1) } }
            .joined(separator: ":")
        return env
    }

    public static let installCommands = [
        "brew install LemonCANDY42/tap/compactlab",
        "uv tool install git+https://github.com/LemonCANDY42/compactlab",
    ]
}
