import AppKit
import CompactlabKit
import SwiftUI

/// The review window: each suggested change with what it does and the data behind it, the exact edit, and Apply.
/// The engine writes only if the plan is still the one shown here (`apply --expect-plan`).
struct ReviewView: View {
    @Bindable var model: AppModel
    @Environment(\.dismissWindow) private var dismissWindow
    @State private var showDiff = false

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 2) {
                Text("Suggested changes").font(.title3.weight(.semibold))
                if let p = model.plan {
                    Text(subtitle(p)).font(.callout).foregroundStyle(.secondary)
                }
            }
            if let msg = model.writeMessage {
                Banner(symbol: "exclamationmark.triangle", tint: .orange, title: L("Nothing was written"), detail: msg) {
                    EmptyView()
                }
            }
            content
            HStack {
                if let p = model.plan, !p.changes.isEmpty {
                    Button("Remind me in a week") {
                        model.snooze(p.changes)
                        dismissWindow(id: "review")
                    }
                }
                Spacer()
                Button("Close") { dismissWindow(id: "review") }
                    .keyboardShortcut(.cancelAction)
                if let p = model.plan, p.changes.count > 1 {
                    Button("Apply all") { Task { await model.apply(nil) } }
                        .buttonStyle(.borderedProminent)
                        .disabled(!model.canChangeSettings || model.isWriting)
                }
            }
        }
        .padding(20)
        .frame(width: 540)
        .task {
            model.clearWriteMessage()
            await model.loadPlan()
        }
    }

    @ViewBuilder private var content: some View {
        if !model.canChangeSettings {
            Text(cannotChange).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        } else if let p = model.plan {
            if p.changes.isEmpty {
                Label("Nothing to change: your settings match the last calibration.", systemImage: "checkmark.circle")
                    .foregroundStyle(.secondary)
            }
            ForEach(p.changes) { c in
                ChangeCard(change: c, busy: model.isWriting) { Task { await model.apply(c) } }
            }
            if !p.skipped.isEmpty {
                Text(L("Not changed: \(p.skipped.joined(separator: "; "))"))
                    .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            if !p.diff.isEmpty {
                DisclosureGroup("What will be written", isExpanded: $showDiff) {
                    ScrollView([.vertical, .horizontal]) {
                        Text(p.diff).font(.caption.monospaced()).textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(6)
                    }
                    .frame(maxHeight: 220)
                    .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 6))
                    ForEach(p.notes, id: \.self) { Text($0).font(.caption).foregroundStyle(.secondary) }
                }
                Text("compactlab keeps a backup of every file it changes. Undo from the menu bar panel, or run `compactlab revert`.")
                    .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
        } else if let e = model.planError {
            Text(e.message).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        } else {
            ProgressView().controlSize(.small)
        }
    }

    private var cannotChange: String {
        if model.result?.demo == true { return L("This is the demo result: there is nothing of yours to change.") }
        if case .incompatible = model.status { return L("This engine speaks another contract version: the app does not change settings with it.") }
        return L("Changing settings from the app needs compactlab 0.2 or later. Until then: `compactlab apply`.")
    }

    private func subtitle(_ p: Plan) -> String {
        var s = L("Profile: \(Format.profile(p.profile))")
        if let at = model.result?.calibration?.at { s += " · " + L("calibrated \(Format.ago(at, now: model.now))") }
        return s
    }
}

private struct ChangeCard: View {
    let change: Plan.Change
    let busy: Bool
    let apply: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .firstTextBaseline) {
                    Text(Panel.changeTitle(harness: change.harness, model: change.model)).fontWeight(.medium)
                    Text(change.harness == "codex" ? "Codex" : "Claude Code").font(.caption).foregroundStyle(.secondary)
                }
                Text("\(Panel.setting(change.old)) → \(Format.tokens(change.new))")
                    .font(.title3.monospacedDigit())
                Text(Panel.impact(change.impact)).font(.callout)
                if let e = Panel.evidence(change.impact) {
                    Text(e).font(.caption).foregroundStyle(.secondary)
                }
                Text("\(change.key) · \((change.file as NSString).abbreviatingWithTildeInPath)")
                    .font(.caption2.monospaced()).foregroundStyle(.tertiary).lineLimit(1).truncationMode(.middle)
                    .textSelection(.enabled)
            }
            Spacer()
            Button("Apply", action: apply).disabled(busy)
        }
        .padding(10)
        .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 8))
        .accessibilityElement(children: .contain)
    }
}
