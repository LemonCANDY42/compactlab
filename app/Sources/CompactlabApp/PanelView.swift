import AppKit
import CompactlabKit
import SwiftUI

/// The window under the menu bar icon: active sessions, the threshold table and the actions.
struct PanelView: View {
    @Bindable var model: AppModel
    @State private var showAllSessions = false
    @Environment(\.openSettings) private var openSettings
    @Environment(\.openWindow) private var openWindow

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
                        changesSection
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
                    Text(L("Calibrated (\(Format.profile(model.result?.profile ?? "balanced")))")).tag("")
                    Divider()
                    Text(Format.profile("cost")).tag("cost")
                    Text(Format.profile("balanced")).tag("balanced")
                    Text(Format.profile("time")).tag("time")
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
            Banner(symbol: "exclamationmark.triangle", tint: .orange, title: L("The engine did not start"), detail: why) {
                Button("Retry") { Task { await model.start() } }
            }
        case .incompatible(let api, let version):
            Banner(
                symbol: "info.circle", tint: .secondary, title: L("Engine \(version) speaks contract \(String(api))"),
                detail: L("This app was built for contract \(String(EngineAPI.version)). It shows what it can and will not change any settings. Update the app and the engine to matching versions.")
            ) { EmptyView() }
        case .ready:
            EmptyView()
        }
        if case .stopped(let why) = model.watchStatus {
            Banner(symbol: "exclamationmark.triangle", tint: .orange, title: L("Active sessions are not updating"), detail: why) {
                Button("Restart") { model.restartWatch() }
            }
        }
        switch model.calibration {
        case .failed(let why):
            Banner(symbol: "exclamationmark.triangle", tint: .orange, title: L("Calibration failed"), detail: why) {
                Button("Retry") { model.recalibrate() }
            }
        case .busy(let why):
            Banner(symbol: "hourglass", tint: .secondary, title: L("Another calibration is running"), detail: why) {
                EmptyView()
            }
        default:
            EmptyView()
        }
        if let err = model.reportError {
            Banner(symbol: "doc.badge.ellipsis", tint: .orange, title: L("The report could not be made"), detail: err) {
                EmptyView()
            }
        }
    }

    // MARK: Sessions

    private var sessionsSection: some View {
        let rows = model.sessionRows
        let shown = showAllSessions ? rows : Array(rows.prefix(Self.firstSessions))
        return VStack(alignment: .leading, spacing: 10) {
            SectionTitle(L("Active sessions"), trailing: model.sessions.map { L("last \(String(Int($0.activeS / 3600))) h") })
            if model.sessions == nil {
                Text(model.watchStatus == .off ? L("Not watching.") : L("Reading…")).foregroundStyle(.secondary)
            } else if rows.isEmpty {
                Text(L("No sessions in the last \(String(Int((model.sessions?.activeS ?? 21_600) / 3600))) hours."))
                    .foregroundStyle(.secondary)
            }
            ForEach(shown) { row in
                SessionRowView(row: row, now: model.now)
            }
            if rows.count > Self.firstSessions {
                Button(showAllSessions ? L("Show fewer") : L("Show \(String(rows.count - Self.firstSessions)) more")) {
                    showAllSessions.toggle()
                }
                .buttonStyle(.link)
                .font(.caption)
            }
            if let n = model.sessions?.unreadable, n > 0 {
                Text(n == 1 ? L("1 log file could not be read.") : L("\(String(n)) log files could not be read."))
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    // MARK: Thresholds

    @ViewBuilder private var thresholdsSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionTitle(L("Thresholds"), trailing: model.result?.demo == true ? L("demo data") : nil)
            if let r = model.result {
                let rows = Panel.thresholdRows(r)
                if rows.isEmpty {
                    Text("No model has enough sessions yet.").foregroundStyle(.secondary)
                }
                ForEach(rows) { row in
                    ThresholdRowView(row: row)
                }
            } else if let e = model.resultError, e.code == "no_result" {
                Text(model.isCalibrating ? L("Calibrating for the first time…") : L("No calibration yet."))
                    .foregroundStyle(.secondary)
            } else if let e = model.resultError {
                Text(e.message).font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    // MARK: Changes

    @ViewBuilder private var changesSection: some View {
        let newest = model.history?.applies.first
        let changes = model.plan?.changes ?? []
        if !changes.isEmpty || newest != nil || model.writeMessage != nil {
            VStack(alignment: .leading, spacing: 8) {
                SectionTitle(L("Changes"))
                if !changes.isEmpty {
                    HStack(alignment: .firstTextBaseline) {
                        if !model.stableChanges.isEmpty {
                            Circle().fill(Color.accentColor).frame(width: 6, height: 6)
                                .accessibilityLabel(L("the last two calibrations agree"))
                        }
                        Text(suggestionSummary(changes)).fixedSize(horizontal: false, vertical: true)
                        Spacer()
                        Button(L("Review…")) { openReview() }
                    }
                }
                if let a = newest { LastChangeView(apply: a, model: model) }
                if let msg = model.writeMessage {
                    Text(msg).font(.caption).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
                }
            }
        } else if model.hello?.canChangeSettings == false, model.result != nil {
            Text("To change settings from here, update compactlab to 0.2 or later. Until then: `compactlab apply`.")
                .font(.caption2).foregroundStyle(.tertiary).fixedSize(horizontal: false, vertical: true)
        }
    }

    private func suggestionSummary(_ changes: [Plan.Change]) -> String {
        let usd = changes.compactMap(\.impact.usdPerDay).reduce(0, +)
        let saving = usd < 0 ? " · " + L("\(Format.usd(usd))/day") : ""
        return (changes.count == 1 ? L("1 suggested change") : L("\(String(changes.count)) suggested changes")) + saving
    }

    private func openReview() {
        NSApp.activate(ignoringOtherApps: true)
        openWindow(id: "review")
    }

    // MARK: Footer

    private var footer: some View {
        HStack(spacing: 14) {
            Button(L("Open report")) {
                Task {
                    if let url = await model.renderReport() { NSWorkspace.shared.open(url) }
                }
            }
            .disabled(model.result == nil)
            Button(L("Recalibrate")) { model.recalibrate() }
                .disabled(!model.status.canWrite || model.isCalibrating)
            Spacer()
            Button(L("Settings…")) {
                NSApp.activate(ignoringOtherApps: true)
                openSettings()
            }
            Button(L("Quit")) { NSApp.terminate(nil) }
        }
        .buttonStyle(.link)
        .font(.callout)
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }
}

// MARK: - Pieces

/// The newest apply: what it changed, whether it still holds, its verification, and Undo.
struct LastChangeView: View {
    let apply: History.Apply
    let model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(alignment: .firstTextBaseline) {
                Text(title).font(.caption).foregroundStyle(.secondary)
                Spacer()
                if apply.inEffect {
                    Button(L("Undo")) { Task { await model.revert(apply.id) } }
                        .buttonStyle(.link).font(.caption)
                        .disabled(model.isWriting || !model.status.canWrite)
                        .help(L("Restore the values from before this change (settings edited since are left alone)"))
                }
            }
            ForEach(apply.changes, id: \.key) { r in
                HStack(alignment: .firstTextBaseline) {
                    Text(Panel.changeLine(r)).monospacedDigit().lineLimit(1)
                    Spacer()
                    Text(Panel.status(r)).font(.caption)
                        .foregroundStyle(r.status == "changed" ? AnyShapeStyle(.orange) : AnyShapeStyle(.secondary))
                }
            }
            if let v = verificationText {
                Text(v).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(8)
        .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 6))
        .accessibilityElement(children: .contain)
    }

    private var title: String {
        let when = apply.at.map { Format.ago($0, now: model.now) } ?? ""
        let who = apply.by == "consent" ? L("auto-apply") : (apply.by == "app" ? L("you, from the app") : L("the command line"))
        return L("Last change · \(when) · by \(who)")
    }

    private var verificationText: String? {
        guard apply.inEffect else { return nil }
        if let v = model.verification, v.id == apply.id {
            return L("Verification: ") + v.groups.map { "\(Panel.changeTitle(harness: $0.harness, model: $0.model)) \(Panel.verdict($0))" }
                .joined(separator: "; ")
        }
        return L("Verified against your logs after a day of use.")
    }
}

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
            parts.append(L("compacted \(Format.duration(ago)) ago"))
        } else if let rem = row.remaining {
            parts.append(rem == 0 ? L("at the threshold") : L("\(Format.tokens(rem)) to compaction"))
        }
        switch row.cache {
        case .warm(let coldIn): parts.append(L("cache warm (cold in \(Format.duration(coldIn)))"))
        case .cold:
            if let n = row.coldRewrite { parts.append(L("cache cold: next send rewrites \(Format.tokens(n))")) } else { parts.append(L("cache cold")) }
        case .unknown: break
        }
        if row.subagents > 0 {
            parts.append(row.subagents == 1 ? L("+1 subagent") : L("+\(String(row.subagents)) subagents"))
        }
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
