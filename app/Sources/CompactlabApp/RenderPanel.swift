import AppKit
import CompactlabKit
import SwiftUI

/// `compactlab --render-panel out.png [--wait seconds]`: start the engine as usual, wait until the panel has its data,
/// draw the panel into a PNG and exit (0 when the engine was usable). For the CI smoke test and for screenshots; no
/// window or menu bar item is shown.
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
        let view = PanelView(model: model).environment(\.colorScheme, .light)
        let host = NSHostingView(rootView: view)
        host.appearance = NSAppearance(named: .aqua)
        host.frame = NSRect(origin: .zero, size: host.fittingSize)
        host.layoutSubtreeIfNeeded()
        let window = NSWindow(contentRect: host.frame, styleMask: [.borderless], backing: .buffered, defer: false)
        window.contentView = host
        window.backgroundColor = .windowBackgroundColor
        host.layoutSubtreeIfNeeded()
        try? await Task.sleep(for: .milliseconds(300))
        guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { return 2 }
        host.cacheDisplay(in: host.bounds, to: rep)
        do {
            try rep.representation(using: .png, properties: [:])?.write(to: file)
        } catch {
            FileHandle.standardError.write(Data("could not write \(file.path): \(error)\n".utf8))
            return 2
        }
        print(model.diagnostics)
        model.shutdown()
        return model.status.isUsable ? 0 : 1
    }
}
