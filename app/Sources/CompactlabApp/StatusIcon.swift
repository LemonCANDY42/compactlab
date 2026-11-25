import AppKit
import CompactlabKit

/// The menu bar icon: a small half-circle gauge, filled to the context share of the fullest recently active session.
/// Template (follows the menu bar's colour) except near a threshold (amber). An engine problem shows an exclamation
/// mark instead of the needle.
enum StatusIcon {
    static func image(_ state: IconState, text: String?) -> NSImage {
        let font = NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .regular)
        let textSize = text.map { ($0 as NSString).size(withAttributes: [.font: font]) } ?? .zero
        let gauge = NSSize(width: 18, height: 16)
        let size = NSSize(width: gauge.width + (text == nil ? 0 : textSize.width + 3), height: gauge.height)
        let amber: Bool = if case .nearThreshold = state { true } else { false }

        let image = NSImage(size: size, flipped: false) { _ in
            let ink: NSColor = amber ? .labelColor : .black
            let center = NSPoint(x: gauge.width / 2, y: 4)
            let radius: CGFloat = 7

            let track = NSBezierPath()
            track.appendArc(withCenter: center, radius: radius, startAngle: 180, endAngle: 0, clockwise: true)
            track.lineWidth = 2
            track.lineCapStyle = .round
            ink.withAlphaComponent(state == .idle ? 0.45 : 0.3).setStroke()
            track.stroke()

            switch state {
            case .engineProblem:
                let mark = "!" as NSString
                let attrs: [NSAttributedString.Key: Any] = [
                    .font: NSFont.systemFont(ofSize: 11, weight: .heavy), .foregroundColor: ink,
                ]
                let s = mark.size(withAttributes: attrs)
                mark.draw(at: NSPoint(x: center.x - s.width / 2, y: 1), withAttributes: attrs)
            case .idle:
                break
            case .active(let f), .nearThreshold(let f):
                let share = CGFloat(min(max(f, 0), 1))
                let fill = NSBezierPath()
                fill.appendArc(
                    withCenter: center, radius: radius, startAngle: 180, endAngle: 180 - 180 * share, clockwise: true)
                fill.lineWidth = 2.4
                fill.lineCapStyle = .round
                (amber ? NSColor.systemOrange : ink).setStroke()
                if share > 0 { fill.stroke() }
                // Needle
                let angle = (180 - 180 * share) * .pi / 180
                let needle = NSBezierPath()
                needle.move(to: center)
                needle.line(to: NSPoint(x: center.x + cos(angle) * (radius - 2.5), y: center.y + sin(angle) * (radius - 2.5)))
                needle.lineWidth = 1.6
                needle.lineCapStyle = .round
                (amber ? NSColor.systemOrange : ink).setStroke()
                needle.stroke()
                NSBezierPath(ovalIn: NSRect(x: center.x - 1.5, y: center.y - 1.5, width: 3, height: 3)).fill()
            }

            if let text {
                (text as NSString).draw(
                    at: NSPoint(x: gauge.width + 3, y: (gauge.height - textSize.height) / 2),
                    withAttributes: [.font: font, .foregroundColor: amber ? NSColor.labelColor : NSColor.black])
            }
            return true
        }
        image.isTemplate = !amber
        return image
    }

    static func accessibilityLabel(_ state: IconState) -> String {
        switch state {
        case .engineProblem: "compactlab: engine problem"
        case .idle: "compactlab: no active sessions"
        case .active(let f): "compactlab: fullest session at \(Int(f * 100))% of its threshold"
        case .nearThreshold(let f): "compactlab: a session is at \(Int(f * 100))% of its threshold"
        }
    }
}
