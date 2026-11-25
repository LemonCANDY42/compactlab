import CoreServices
import Foundation

/// FSEvents on the harnesses' log directories, coalesced over `latency` seconds. The callback runs on a background
/// queue. Nothing polls: with no writes there are no callbacks.
public final class FileWatcher: @unchecked Sendable {
    /// What the stream's callback reaches. The stream retains it, so a callback still running on the queue when the
    /// watcher is replaced never touches freed memory.
    private final class Callback {
        let run: @Sendable () -> Void
        init(_ run: @escaping @Sendable () -> Void) { self.run = run }
    }

    private let onChange: Callback
    private var stream: FSEventStreamRef?
    private let queue = DispatchQueue(label: "compactlab.fsevents", qos: .utility)
    public private(set) var paths: [String] = []

    public init(onChange: @escaping @Sendable () -> Void) {
        self.onChange = Callback(onChange)
    }

    deinit { stop() }

    /// Watch `paths` (replacing what was watched). Returns false when FSEvents refused them.
    @discardableResult
    public func watch(_ paths: [String], latency: TimeInterval = 2) -> Bool {
        stop()
        self.paths = paths
        guard !paths.isEmpty else { return true }
        var ctx = FSEventStreamContext(
            version: 0, info: Unmanaged.passUnretained(onChange).toOpaque(),
            retain: { info in
                guard let info else { return nil }
                _ = Unmanaged<Callback>.fromOpaque(info).retain()
                return info
            },
            release: { info in
                guard let info else { return }
                Unmanaged<Callback>.fromOpaque(info).release()
            },
            copyDescription: nil)
        let callback: FSEventStreamCallback = { _, info, _, _, _, _ in
            guard let info else { return }
            Unmanaged<Callback>.fromOpaque(info).takeUnretainedValue().run()
        }
        let flags = FSEventStreamCreateFlags(kFSEventStreamCreateFlagNoDefer | kFSEventStreamCreateFlagWatchRoot)
        guard
            let s = FSEventStreamCreate(
                nil, callback, &ctx, paths as CFArray, FSEventStreamEventId(kFSEventStreamEventIdSinceNow), latency,
                flags)
        else { return false }
        FSEventStreamSetDispatchQueue(s, queue)
        guard FSEventStreamStart(s) else {
            FSEventStreamInvalidate(s)
            FSEventStreamRelease(s)
            return false
        }
        stream = s
        return true
    }

    public func stop() {
        guard let s = stream else { return }
        FSEventStreamStop(s)
        FSEventStreamInvalidate(s)
        FSEventStreamRelease(s)
        stream = nil
    }
}
