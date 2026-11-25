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
            loginError = "Could not change the login item: \(error.localizedDescription)"
        }
        openAtLogin = SMAppService.mainApp.status == .enabled
    }
}

private struct EngineSettings: View {
    @Bindable var model: AppModel
    @State private var copied = false

    var body: some View {
        Form {
            LabeledContent("Status") { Text(statusText).textSelection(.enabled) }
            LabeledContent("In use") {
                Text(model.executable?.path ?? "not found").font(.callout.monospaced()).textSelection(.enabled)
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
        case .starting: "starting"
        case .missing: "not found"
        case .failed(let why): "failed: \(why)"
        case .incompatible(let api, let v): "\(v) (contract \(api); read-only)"
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
