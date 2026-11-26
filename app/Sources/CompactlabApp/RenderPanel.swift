import AppKit
import CompactlabKit
import SwiftUI

/// `compactlab --render-panel out.png [--render-review review.png] [--wait seconds]`: start the engine as usual, wait
/// until the panel has its data, draw the panel (and the review window) into PNGs and exit (0 when the engine was
/// usable). For the CI smoke test and for screenshots; no window or menu bar item is shown. `-AppleLanguages
/// '(zh-Hans)'` draws another language.
enum RenderPanel {
    static func requested() -> (URL, Double)? {
        let args = CommandLine.arguments
        guard let i = args.firstIndex(of: "--render-panel"), i + 1 < args.count else { return nil }
        var wait = 20.0
        if let j = args.firstIndex(of: "--wait"), j + 1 < args.count, let w = Double(args[j + 1]) { wait = w }
        return (URL(fileURLWithPath: args[i + 1]), wait)
    }

    @MainActor
    static func run(to file: URL, wait: Double) async -> Int32 {
        let model = AppModel()
        model.scheduling = false
        await model.start()
        let end = Date().addingTimeInterval(wait)
        while Date() < end, model.status.isUsable, model.sessions == nil || (model.result == nil && model.resultError == nil) {
            try? await Task.sleep(for: .milliseconds(100))
        }
        if let review = value(after: "--render-review") {
            guard await draw(ReviewView(model: model), to: URL(fileURLWithPath: review)) else { return 2 }
        }
        guard await draw(PanelView(model: model), to: file) else { return 2 }
        print(model.diagnostics)
        model.shutdown()
        return model.status.isUsable ? 0 : 1
    }

    static func value(after flag: String) -> String? {
        let args = CommandLine.arguments
        guard let i = args.firstIndex(of: flag), i + 1 < args.count else { return nil }
        return args[i + 1]
    }

    @MainActor
    static func draw(_ content: some View, to file: URL) async -> Bool {
        let view = content.environment(\.colorScheme, .light)
        let host = NSHostingView(rootView: view)
        host.appearance = NSAppearance(named: .aqua)
        host.frame = NSRect(origin: .zero, size: host.fittingSize)
        host.layoutSubtreeIfNeeded()
        let window = NSWindow(contentRect: host.frame, styleMask: [.borderless], backing: .buffered, defer: false)
        window.contentView = host
        window.backgroundColor = .windowBackgroundColor
        host.layoutSubtreeIfNeeded()
        try? await Task.sleep(for: .milliseconds(300))
        guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { return false }
        host.cacheDisplay(in: host.bounds, to: rep)
        do {
            try rep.representation(using: .png, properties: [:])?.write(to: file)
            return true
        } catch {
            FileHandle.standardError.write(Data("could not write \(file.path): \(error)\n".utf8))
            return false
        }
    }
}
