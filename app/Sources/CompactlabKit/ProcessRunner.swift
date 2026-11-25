import Foundation

/// What a one-shot engine command left behind.
public struct ProcessOutput: Sendable {
    public var status: Int32
    public var stdout: Data
    public var stderr: Data
    /// Stopped because it ran past its timeout.
    public var timedOut: Bool
    /// Stopped because the task that ran it was cancelled.
    public var cancelled: Bool

    /// The last line of stderr, for a status message (tracebacks end with the exception).
    public var stderrTail: String {
        let s = String(decoding: stderr.suffix(4096), as: UTF8.self)
        return s.split(whereSeparator: \.isNewline).last.map(String.init) ?? ""
    }
}

/// Runs one-shot engine commands (`hello`, `result`, `calibrate`, `report`) as child processes that exit when done,
/// so the memory a calibration takes goes back to the system. Below user-interactive priority, with a timeout, and
/// stopped when the calling task is cancelled.
public enum ProcessRunner {
    public static func run(
        _ executable: URL,
        _ arguments: [String],
        environment: [String: String]? = nil,
        timeout: Duration,
        qos: QualityOfService = .utility
    ) async throws -> ProcessOutput {
        let process = Process()
        process.executableURL = executable
        process.arguments = arguments
        if let environment { process.environment = environment }
        process.qualityOfService = qos
        let out = Pipe(), err = Pipe()
        process.standardOutput = out
        process.standardError = err
        process.standardInput = FileHandle.nullDevice

        let box = OutputBox()
        let group = DispatchGroup()
        group.enter()
        process.terminationHandler = { _ in group.leave() }
        do {
            try process.run()
        } catch {
            group.leave()
            throw error
        }
        // Read both pipes to the end while the process runs, so a large result cannot fill a pipe and stall it.
        for (handle, isOut) in [(out.fileHandleForReading, true), (err.fileHandleForReading, false)] {
            group.enter()
            DispatchQueue.global(qos: .utility).async {
                let data = handle.readDataToEndOfFile()
                box.set(data, stdout: isOut)
                group.leave()
            }
        }

        let proc = ProcessRef(process)
        let seconds = Double(timeout.components.seconds) + Double(timeout.components.attoseconds) / 1e18
        return await withTaskCancellationHandler {
            await withCheckedContinuation { (cont: CheckedContinuation<ProcessOutput, Never>) in
                let timer = DispatchWorkItem {
                    box.markTimedOut()
                    stop(proc)
                }
                DispatchQueue.global().asyncAfter(deadline: .now() + seconds, execute: timer)
                group.notify(queue: .global()) {
                    timer.cancel()
                    cont.resume(returning: box.output(status: proc.process.terminationStatus))
                }
            }
        } onCancel: {
            box.markCancelled()
            stop(proc)
        }
    }

    /// SIGTERM, then SIGKILL if it is still there five seconds later.
    static func stop(_ ref: ProcessRef) {
        guard ref.process.isRunning else { return }
        ref.process.terminate()
        let pid = ref.process.processIdentifier
        DispatchQueue.global().asyncAfter(deadline: .now() + 5) {
            if ref.process.isRunning { kill(pid, SIGKILL) }
        }
    }
}

/// `Process` is thread-safe for what is done with it here (state queries, signals) but not marked `Sendable`.
final class ProcessRef: @unchecked Sendable {
    let process: Process
    init(_ process: Process) { self.process = process }
}

private final class OutputBox: @unchecked Sendable {
    private let lock = NSLock()
    private var stdout = Data(), stderr = Data()
    private var timedOut = false, cancelled = false

    func set(_ data: Data, stdout isOut: Bool) {
        lock.withLock { if isOut { stdout = data } else { stderr = data } }
    }

    func markTimedOut() { lock.withLock { timedOut = true } }
    func markCancelled() { lock.withLock { cancelled = true } }

    func output(status: Int32) -> ProcessOutput {
        lock.withLock {
            ProcessOutput(status: status, stdout: stdout, stderr: stderr, timedOut: timedOut, cancelled: cancelled)
        }
    }
}
