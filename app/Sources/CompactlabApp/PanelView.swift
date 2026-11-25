import AppKit
import CompactlabKit
import SwiftUI

/// The window under the menu bar icon: active sessions, the threshold table and the actions.
struct PanelView: View {
    @Bindable var model: AppModel
    @State private var showAllSessions = false
    @Environment(\.openSettings) private var openSettings

    private static let firstSessions = 4

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    banners
                    if model.status.isUsable {
                        sessionsSection
                        thresholdsSection
                    }
                }
                .padding(12)
            }
            .frame(maxHeight: 520)
            Divider()
            footer
        }
        .frame(width: 380)
    }

    // MARK: Header

    private var header: some View {
        HStack(spacing: 8) {
            Text("compactlab").font(.headline)
            Spacer()
            if model.result != nil {
                Picker("Profile", selection: $model.displayProfile) {
                    Text("Calibrated (\(model.result?.profile ?? "balanced"))").tag("")
                    Divider()
                    Text("cost").tag("cost")
                    Text("balanced").tag("balanced")
                    Text("time").tag("time")
                }
                .labelsHidden()
                .pickerStyle(.menu)
                .fixedSize()
                .help("Which profile the threshold table shows")
            }
            calibrationStatus
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }

    @ViewBuilder private var calibrationStatus: some View {
        if model.isCalibrating {
            ProgressView().controlSize(.small)
            Button("Cancel") { model.cancelCalibration() }.buttonStyle(.link)
        } else if let at = model.result?.calibration?.at ?? model.hello?.service.lastCalibrated {
            Text(Format.ago(at, now: model.now))
                .font(.caption).foregroundStyle(.secondary)
                .help("Last calibration")
        }
    }

    // MARK: Banners

    @ViewBuilder private var banners: some View {
        switch model.status {
        case .starting:
            HStack {
                ProgressView().controlSize(.small)
                Text("Starting the engine…").foregroundStyle(.secondary)
            }
        case .missing:
            MissingEngineView(retry: { Task { await model.start() } })
        case .failed(let why):
            Banner(symbol: "exclamationmark.triangle", tint: .orange, title: "The engine did not start", detail: why) {
                Button("Retry") { Task { await model.start() } }
            }
        case .incompatible(let api, let version):
            Banner(
                symbol: "info.circle", tint: .secondary, title: "Engine \(version) speaks contract \(api)",
                detail: "This app was built for contract \(EngineAPI.version). It shows what it can and will not change any settings. Update the app and the engine to matching versions."
            ) { EmptyView() }
        case .ready:
            EmptyView()
        }
        if case .stopped(let why) = model.watchStatus {
            Banner(symbol: "exclamationmark.triangle", tint: .orange, title: "Active sessions are not updating", detail: why) {
                Button("Restart") { model.restartWatch() }
            }
        }
        switch model.calibration {
        case .failed(let why):
            Banner(symbol: "exclamationmark.triangle", tint: .orange, title: "Calibration failed", detail: why) {
                Button("Retry") { model.recalibrate() }
            }
        case .busy(let why):
            Banner(symbol: "hourglass", tint: .secondary, title: "Another calibration is running", detail: why) {
                EmptyView()
            }
        default:
            EmptyView()
        }
        if let err = model.reportError {
            Banner(symbol: "doc.badge.ellipsis", tint: .orange, title: "The report could not be made", detail: err) {
                EmptyView()
            }
        }
    }

    // MARK: Sessions

    private var sessionsSection: some View {
        let rows = model.sessionRows
        let shown = showAllSessions ? rows : Array(rows.prefix(Self.firstSessions))
        return VStack(alignment: .leading, spacing: 10) {
            SectionTitle("Active sessions", trailing: model.sessions.map { "last \(Int($0.activeS / 3600)) h" })
            if model.sessions == nil {
                Text(model.watchStatus == .off ? "Not watching." : "Reading…").foregroundStyle(.secondary)
            } else if rows.isEmpty {
                Text("No sessions in the last \(Int((model.sessions?.activeS ?? 21_600) / 3600)) hours.")
                    .foregroundStyle(.secondary)
            }
            ForEach(shown) { row in
                SessionRowView(row: row, now: model.now)
            }
            if rows.count > Self.firstSessions {
                Button(showAllSessions ? "Show fewer" : "Show \(rows.count - Self.firstSessions) more") {
                    showAllSessions.toggle()
                }
                .buttonStyle(.link)
                .font(.caption)
            }
            if let n = model.sessions?.unreadable, n > 0 {
                Text("\(n) log file\(n == 1 ? "" : "s") could not be read.").font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    // MARK: Thresholds

    @ViewBuilder private var thresholdsSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionTitle("Thresholds", trailing: model.result?.demo == true ? "demo data" : nil)
            if let r = model.result {
                let rows = Panel.thresholdRows(r)
                if rows.isEmpty {
                    Text("No model has enough sessions yet.").foregroundStyle(.secondary)
                }
                ForEach(rows) { row in
                    ThresholdRowView(row: row)
                }
                Text("Read-only: change settings with `compactlab apply` for now.")
                    .font(.caption2).foregroundStyle(.tertiary)
            } else if let e = model.resultError, e.code == "no_result" {
                Text(model.isCalibrating ? "Calibrating for the first time…" : "No calibration yet.")
                    .foregroundStyle(.secondary)
            } else if let e = model.resultError {
                Text(e.message).font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    // MARK: Footer

    private var footer: some View {
        HStack(spacing: 14) {
            Button("Open report") {
                Task {
                    if let url = await model.renderReport() { NSWorkspace.shared.open(url) }
                }
            }
            .disabled(model.result == nil)
            Button("Recalibrate") { model.recalibrate() }
                .disabled(!model.status.isUsable || model.isCalibrating)
            Spacer()
            Button("Settings…") {
                NSApp.activate(ignoringOtherApps: true)
                openSettings()
            }
            Button("Quit") { NSApp.terminate(nil) }
        }
        .buttonStyle(.link)
        .font(.callout)
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }
}

// MARK: - Pieces

struct SectionTitle: View {
    let title: String
    let trailing: String?

    init(_ title: String, trailing: String? = nil) {
        self.title = title
        self.trailing = trailing
    }

    var body: some View {
        HStack {
            Text(title).font(.subheadline.weight(.semibold))
            Spacer()
            if let trailing { Text(trailing).font(.caption).foregroundStyle(.secondary) }
        }
    }
}

struct SessionRowView: View {
    let row: SessionRow
    let now: Date

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(alignment: .firstTextBaseline) {
                Text(row.title).fontWeight(.medium).lineLimit(1)
                Text(row.model).foregroundStyle(.secondary).lineLimit(1)
                Spacer()
                if let ctx = row.ctx, let th = row.threshold {
                    Text("\(Format.tokens(ctx)) / \(Format.tokens(th))").monospacedDigit()
                } else if let th = row.threshold {
                    Text("– / \(Format.tokens(th))").monospacedDigit().foregroundStyle(.secondary)
                }
            }
            if let f = row.fraction {
                ProgressView(value: min(f, 1))
                    .tint(f >= Panel.warnAt ? .orange : .accentColor)
            }
            HStack(spacing: 4) {
                Text(detail).font(.caption).foregroundStyle(.secondary)
                Spacer()
                if row.coldRewrite != nil {
                    Button("Copy /compact") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString("/compact", forType: .string)
                    }
                    .buttonStyle(.link)
                    .font(.caption)
                    .help("Compacting before the next message avoids writing the whole context to cache again")
                }
            }
        }
        .accessibilityElement(children: .combine)
    }

    private var detail: String {
        var parts: [String] = []
        if let ago = row.compactedAgo {
            parts.append("compacted \(Format.duration(ago)) ago")
        } else if let rem = row.remaining {
            parts.append(rem == 0 ? "at the threshold" : "\(Format.tokens(rem)) to compaction")
        }
        switch row.cache {
        case .warm(let coldIn): parts.append("cache warm (cold in \(Format.duration(coldIn)))")
        case .cold:
            if let n = row.coldRewrite { parts.append("cache cold: next send rewrites \(Format.tokens(n))") } else { parts.append("cache cold") }
        case .unknown: break
        }
        if row.subagents > 0 { parts.append("+\(row.subagents) subagent\(row.subagents == 1 ? "" : "s")") }
        return parts.joined(separator: " · ")
    }
}

struct ThresholdRowView: View {
    let row: ThresholdRow

    var body: some View {
        VStack(alignment: .leading, spacing: 1) {
            HStack(alignment: .firstTextBaseline) {
                if row.suggestsChange {
                    Circle().fill(Color.accentColor).frame(width: 6, height: 6).accessibilityLabel("suggests a change")
                }
                Text(row.title).lineLimit(1)
                Spacer()
                Text(row.value).monospacedDigit().fontWeight(row.suggestsChange ? .semibold : .regular)
            }
            Text(row.detail).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
        .accessibilityElement(children: .combine)
    }
}

struct Banner<Actions: View>: View {
    let symbol: String
    let tint: Color
    let title: String
    let detail: String
    @ViewBuilder let actions: () -> Actions

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: symbol).foregroundStyle(tint)
            VStack(alignment: .leading, spacing: 4) {
                Text(title).fontWeight(.medium)
                Text(detail).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
                actions().buttonStyle(.link).font(.caption)
            }
        }
        .padding(8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 6))
    }
}

struct MissingEngineView: View {
    let retry: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Label("compactlab is not installed", systemImage: "shippingbox").fontWeight(.medium)
            Text("The app reads your sessions through the compactlab command-line tool. Install it with one of:")
                .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            ForEach(EngineLocator.installCommands, id: \.self) { cmd in
                HStack {
                    Text(cmd).font(.caption.monospaced()).textSelection(.enabled).lineLimit(1).truncationMode(.middle)
                    Spacer()
                    Button {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(cmd, forType: .string)
                    } label: {
                        Image(systemName: "doc.on.doc")
                    }
                    .buttonStyle(.borderless)
                    .help("Copy")
                }
            }
            Text("Installed somewhere else? Set its path in Settings.").font(.caption).foregroundStyle(.secondary)
            Button("Check again", action: retry).buttonStyle(.link).font(.caption)
        }
        .padding(8)
        .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 6))
    }
}
