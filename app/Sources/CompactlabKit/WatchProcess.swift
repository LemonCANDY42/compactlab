import Foundation

/// The resident `compactlab engine watch` process: JSON Lines on stdout, one command per line on stdin. Its events
/// (each complete line, then its exit) arrive in order on `events`. Closing stdin makes the engine exit, so it never
/// outlives the app even when the app is killed.
public final class WatchProcess: @unchecked Sendable {
    public enum Event: Sendable, Equatable {
        case line(Data)
        case exited(status: Int32)
    }

    public let events: AsyncStream<Event>
    private let continuation: AsyncStream<Event>.Continuation
    private let process = Process()
    private let stdin = Pipe(), stdout = Pipe(), stderr = Pipe()
    private let lock = NSLock()
    private var buffer = Data()
    private var stderrTail = Data()
    private var eof = false
    private var status: Int32?
    private var finished = false

    public init(executable: URL, arguments: [String], environment: [String: String]? = nil) {
        (events, continuation) = AsyncStream.makeStream(of: Event.self)
        process.executableURL = executable
        process.arguments = arguments
        if let environment { process.environment = environment }
        process.qualityOfService = .utility
        process.standardInput = stdin
        process.standardOutput = stdout
        process.standardError = stderr
    }

    public var pid: Int32 { process.processIdentifier }

    /// The end of what the engine wrote to stderr (a traceback, for a crash).
    public var lastStderr: String {
        lock.withLock { String(decoding: stderrTail, as: UTF8.self) }
    }

    public func start() throws {
        stdout.fileHandleForReading.readabilityHandler = { [weak self] h in
            let data = h.availableData
            guard let self else { return }
            if data.isEmpty {
                h.readabilityHandler = nil
                self.lock.withLock { self.eof = true }
                self.flush()
            } else {
                self.lock.withLock { self.buffer.append(data) }
                self.flush()
            }
        }
        stderr.fileHandleForReading.readabilityHandler = { [weak self] h in
            let data = h.availableData
            if data.isEmpty {
                h.readabilityHandler = nil
                return
            }
            guard let self else { return }
            self.lock.withLock { self.stderrTail = (self.stderrTail + data).suffix(4096) }
        }
        process.terminationHandler = { [weak self] p in
            guard let self else { return }
            self.lock.withLock { self.status = p.terminationStatus }
            self.flush()
        }
        do {
            try process.run()
        } catch {
            stdout.fileHandleForReading.readabilityHandler = nil
            stderr.fileHandleForReading.readabilityHandler = nil
            continuation.finish()
            throw error
        }
    }

    /// Send one command (`refresh`, `snapshot`, `ping`, `quit`). A no-op once the process is gone.
    public func send(_ command: String) {
        guard process.isRunning else { return }
        // The throwing write: the other one raises an Objective-C exception on a closed pipe. The app ignores SIGPIPE.
        try? stdin.fileHandleForWriting.write(contentsOf: Data((command + "\n").utf8))
    }

    /// Ask it to quit, then make sure: SIGTERM after two seconds, SIGKILL five seconds after that.
    public func stop() {
        send("quit")
        try? stdin.fileHandleForWriting.close()
        let ref = ProcessRef(process)
        DispatchQueue.global().asyncAfter(deadline: .now() + 2) {
            ProcessRunner.stop(ref)
        }
    }

    /// Yield every complete line; once stdout is at its end and the process has exited, yield the exit and finish.
    private func flush() {
        var lines: [Data] = []
        var exit: Int32?
        lock.withLock {
            while let nl = buffer.firstIndex(of: UInt8(ascii: "\n")) {
                let line = buffer[buffer.startIndex..<nl]
                if !line.isEmpty { lines.append(Data(line)) }
                buffer.removeSubrange(buffer.startIndex...nl)
            }
            if eof, !buffer.isEmpty {  // a last line without a newline
                lines.append(buffer)
                buffer.removeAll()
            }
            if eof, let status, !finished {
                finished = true
                exit = status
            }
        }
        for line in lines { continuation.yield(.line(line)) }
        if let exit {
            continuation.yield(.exited(status: exit))
            continuation.finish()
        }
    }
}

/// When to restart a watch process that exited on its own: right away, unless it has now exited `maxExits` times
/// within `window` seconds; then it stays stopped until the user restarts it.
public struct RestartPolicy: Sendable {
    public var window: TimeInterval
    public var maxExits: Int
    private var exits: [Date] = []

    public init(window: TimeInterval = 60, maxExits: Int = 3) {
        self.window = window
        self.maxExits = maxExits
    }

    /// Record an exit at `now`; true when the process should be started again.
    public mutating func shouldRestart(at now: Date) -> Bool {
        exits = exits.filter { now.timeIntervalSince($0) < window } + [now]
        return exits.count < maxExits
    }

    public mutating func reset() { exits = [] }
}
