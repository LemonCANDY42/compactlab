import CoreServices
import Foundation

/// FSEvents on the harnesses' log directories, coalesced over `latency` seconds. The callback runs on a background
/// queue. Nothing polls: with no writes there are no callbacks.
public final class FileWatcher: @unchecked Sendable {
    private let onChange: @Sendable () -> Void
    private var stream: FSEventStreamRef?
    private let queue = DispatchQueue(label: "compactlab.fsevents", qos: .utility)
    public private(set) var paths: [String] = []

    public init(onChange: @escaping @Sendable () -> Void) {
        self.onChange = onChange
    }

    deinit { stop() }

    /// Watch `paths` (replacing what was watched). Returns false when FSEvents refused them.
    @discardableResult
    public func watch(_ paths: [String], latency: TimeInterval = 2) -> Bool {
        stop()
        self.paths = paths
        guard !paths.isEmpty else { return true }
        var ctx = FSEventStreamContext(
            version: 0, info: Unmanaged.passUnretained(self).toOpaque(), retain: nil, release: nil,
            copyDescription: nil)
        let callback: FSEventStreamCallback = { _, info, _, _, _, _ in
            guard let info else { return }
            Unmanaged<FileWatcher>.fromOpaque(info).takeUnretainedValue().onChange()
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
