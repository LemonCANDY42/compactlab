import AppKit
import CompactlabKit
import ServiceManagement
import SwiftUI

struct SettingsView: View {
    @Bindable var model: AppModel

    var body: some View {
        TabView {
            GeneralSettings(model: model)
                .tabItem { Label("General", systemImage: "gearshape") }
            ChangesSettings(model: model)
                .tabItem { Label("Changes", systemImage: "slider.horizontal.3") }
            EngineSettings(model: model)
                .tabItem { Label("Engine", systemImage: "terminal") }
        }
        .frame(width: 460)
        .padding(20)
    }
}

private struct GeneralSettings: View {
    @Bindable var model: AppModel
    @State private var openAtLogin = SMAppService.mainApp.status == .enabled
    @State private var loginError: String?

    var body: some View {
        Form {
            Toggle("Open at login", isOn: $openAtLogin)
                .onChange(of: openAtLogin) { _, on in setLogin(on) }
            if let loginError {
                Text(loginError).font(.caption).foregroundStyle(.orange)
            }
            Toggle("Show the fullest session's percentage in the menu bar", isOn: $model.showPercent)
            Toggle("Show project names", isOn: $model.showProjects)
            Text("Project names (the last part of each session's folder) are shown only in this panel and never written to a file.")
                .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
    }

    private func setLogin(_ on: Bool) {
        do {
            if on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
            loginError = nil
        } catch {
            loginError = L("Could not change the login item: \(error.localizedDescription)")
        }
        openAtLogin = SMAppService.mainApp.status == .enabled
    }
}

private struct ChangesSettings: View {
    @Bindable var model: AppModel

    var body: some View {
        Form {
            Section {
                Toggle(
                    "Apply suggestions automatically",
                    isOn: Binding(get: { model.autoApply }, set: { on in Task { await model.setAutoApply(on) } })
                )
                .disabled(!model.canChangeSettings || model.isWriting)
                Text("Claude Code only. A threshold moves only after 7 days and 500 turns of data, by at most 100k, at most once a week, and never over a value someone else set. Each change is notified and can be undone. Off: nothing is written until you click Apply.")
                    .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            Section("Notify me about") {
                Toggle("Suggestions the last two calibrations agree on", isOn: $model.notifySuggestions)
                Toggle("Changes: auto-apply, settings edited elsewhere, verification results", isOn: $model.notifyChanges)
                Toggle("Calibration failing three times in a row", isOn: $model.notifyProblems)
                Text("macOS asks once whether compactlab may notify you; change that later in System Settings → Notifications.")
                    .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

private struct EngineSettings: View {
    @Bindable var model: AppModel
    @State private var copied = false

    var body: some View {
        Form {
            LabeledContent("Status") { Text(statusText).textSelection(.enabled) }
            LabeledContent("In use") {
                Text(model.executable?.path ?? L("not found")).font(.callout.monospaced()).textSelection(.enabled)
            }
            HStack {
                TextField("Path to compactlab", text: $model.enginePath, prompt: Text("found automatically"))
                Button("Choose…") { choose() }
            }
            Text("Searched when empty: your PATH, ~/.local/bin, /opt/homebrew/bin, /usr/local/bin.")
                .font(.caption).foregroundStyle(.secondary)
            HStack {
                Button("Restart engine") { Task { await model.start() } }
                Button(copied ? "Copied" : "Copy diagnostics") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(model.diagnostics, forType: .string)
                    copied = true
                }
                .help("Versions, paths and states; no session content or project names")
            }
        }
    }

    private var statusText: String {
        switch model.status {
        case .starting: L("starting")
        case .missing: L("not found")
        case .failed(let why): L("failed: \(why)")
        case .incompatible(let api, let v): L("\(v) (contract \(String(api)); read-only)")
        case .ready(let v): "compactlab \(v)"
        }
    }

    private func choose() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.showsHiddenFiles = true
        panel.directoryURL = URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent(".local/bin")
        if panel.runModal() == .OK, let url = panel.url {
            model.enginePath = url.path
            Task { await model.start() }
        }
    }
}
