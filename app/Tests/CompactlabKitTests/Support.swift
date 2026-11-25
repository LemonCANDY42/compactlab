import Foundation

/// The engine contract fixtures shared with the Python tests (`tests/contract/` at the repository root).
enum Fixture {
    static let dir = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().appendingPathComponent("tests/contract")

    static func data(_ name: String) throws -> Data {
        try Data(contentsOf: dir.appendingPathComponent("\(name).json"))
    }

    /// The fixture on one line, as `watch` prints it.
    static func line(_ name: String) throws -> String {
        let obj = try JSONSerialization.jsonObject(with: data(name))
        return String(decoding: try JSONSerialization.data(withJSONObject: obj, options: .sortedKeys), as: UTF8.self)
    }
}

/// A scratch directory removed when the value goes away.
final class TempDir {
    let url: URL

    init() throws {
        url = FileManager.default.temporaryDirectory.appendingPathComponent("compactlab-tests-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
    }

    deinit { try? FileManager.default.removeItem(at: url) }

    /// Write an executable shell script.
    func script(_ name: String, _ body: String) throws -> URL {
        let f = url.appendingPathComponent(name)
        try ("#!/bin/sh\n" + body).write(to: f, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: f.path)
        return f
    }

    func write(_ name: String, _ text: String) throws -> URL {
        let f = url.appendingPathComponent(name)
        try text.write(to: f, atomically: true, encoding: .utf8)
        return f
    }
}

/// Wait until `condition` holds or `timeout` passes.
@MainActor
func eventually(timeout: Duration = .seconds(10), _ condition: () -> Bool) async -> Bool {
    let end = ContinuousClock.now + timeout
    while ContinuousClock.now < end {
        if condition() { return true }
        try? await Task.sleep(for: .milliseconds(50))
    }
    return condition()
}
