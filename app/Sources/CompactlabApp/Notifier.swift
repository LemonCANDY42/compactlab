import AppKit
import CompactlabKit
@preconcurrency import UserNotifications

/// Posts the model's notices as macOS notifications, with Review and Undo actions. Only in the packaged app:
/// UNUserNotificationCenter needs a bundle (it fails under `swift run` and in tests). Permission is asked when the
/// first notice is posted, not at launch.
@MainActor
final class Notifier: NSObject, UNUserNotificationCenterDelegate {
    static var available: Bool { Bundle.main.bundleURL.pathExtension == "app" && Bundle.main.bundleIdentifier != nil }

    private let model: AppModel
    private var asked = false

    init(model: AppModel) {
        self.model = model
        super.init()
    }

    func install() {
        guard Self.available else { return }
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        let review = UNNotificationAction(identifier: "review", title: L("Review"), options: [.foreground])
        let undo = UNNotificationAction(identifier: "undo", title: L("Undo"), options: [.destructive])
        center.setNotificationCategories([
            UNNotificationCategory(identifier: "review", actions: [review], intentIdentifiers: []),
            UNNotificationCategory(identifier: "undo", actions: [undo], intentIdentifiers: []),
        ])
        model.post = { [weak self] notice in self?.post(notice) }
    }

    private func post(_ n: Notice) {
        Task {
            let center = UNUserNotificationCenter.current()
            if !asked {
                asked = true
                _ = try? await center.requestAuthorization(options: [.alert, .sound])
            }
            let content = UNMutableNotificationContent()
            content.title = n.title
            content.body = n.body
            switch n.action {
            case .review:
                content.categoryIdentifier = "review"
            case .undo(let id):
                content.categoryIdentifier = "undo"
                content.userInfo = ["undo": id]
            case .none:
                break
            }
            try? await center.add(UNNotificationRequest(identifier: n.id, content: content, trigger: nil))
        }
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse
    ) async {
        let action = response.actionIdentifier
        let category = response.notification.request.content.categoryIdentifier
        let undo = response.notification.request.content.userInfo["undo"] as? String
        await MainActor.run {
            if action == "undo", let undo {
                Task { await model.revert(undo) }
            } else if action == "review" || (action == UNNotificationDefaultActionIdentifier && category == "review") {
                model.requestReview()
            }
        }
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter, willPresent notification: UNNotification
    ) async -> UNNotificationPresentationOptions {
        [.banner, .list]
    }
}
