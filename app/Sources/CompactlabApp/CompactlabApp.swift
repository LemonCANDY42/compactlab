import AppKit
import CompactlabKit
import SwiftUI

@main
struct CompactlabMenuBarApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    var body: some Scene {
        MenuBarExtra {
            PanelView(model: delegate.model)
        } label: {
            MenuBarLabel(model: delegate.model)
        }
        .menuBarExtraStyle(.window)

        Window(L("Review changes"), id: "review") {
            ReviewView(model: delegate.model)
        }
        .windowResizability(.contentSize)
        .defaultPosition(.center)

        Settings {
            SettingsView(model: delegate.model)
        }
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    let model = AppModel()
    private lazy var notifier = Notifier(model: model)

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Writing to an engine that just exited must fail with EPIPE, not kill the app.
        signal(SIGPIPE, SIG_IGN)
        NSApp.setActivationPolicy(.accessory)  // also set by LSUIElement in the bundle; this covers `swift run`
        if let (file, wait) = RenderPanel.requested() {
            Task {
                let status = await RenderPanel.run(to: file, wait: wait)
                exit(status)
            }
            return
        }
        notifier.install()
        NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didWakeNotification, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.model.didWake() }
        }
        Task { await model.start() }
    }

    func applicationWillTerminate(_ notification: Notification) {
        model.shutdown()
    }
}

/// Always alive while the app runs, so it also opens the review window when a notification asks for it.
struct MenuBarLabel: View {
    let model: AppModel
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        let state = model.icon
        let percent = model.showPercent ? state.fraction.map { "\(Int(($0 * 100).rounded()))%" } : nil
        Image(nsImage: StatusIcon.image(state, text: percent, dot: model.hasPending))
            .accessibilityLabel(StatusIcon.accessibilityLabel(state, pending: model.hasPending))
            .onChange(of: model.reviewRequest) {
                NSApp.activate(ignoringOtherApps: true)
                openWindow(id: "review")
            }
    }
}
