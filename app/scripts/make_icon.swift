// Draw the app icon (the menu bar gauge on a rounded square) and write an .icns.
//
//   swift app/scripts/make_icon.swift OUT.icns
//
// Generated at packaging time, so no binary image lives in the repository.
import AppKit

let out = URL(fileURLWithPath: CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "AppIcon.icns")
let fm = FileManager.default
let set = fm.temporaryDirectory.appendingPathComponent("compactlab-\(UUID().uuidString).iconset")
try fm.createDirectory(at: set, withIntermediateDirectories: true)
defer { try? fm.removeItem(at: set) }

func draw(_ px: Int) -> Data {
    let s = CGFloat(px)
    let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px, bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
        isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    // The macOS icon grid: an 824/1024 rounded square, centred.
    let inset = s * 100 / 1024
    let body = NSRect(x: inset, y: inset, width: s - 2 * inset, height: s - 2 * inset)
    let shape = NSBezierPath(roundedRect: body, xRadius: body.width * 0.225, yRadius: body.width * 0.225)
    NSGradient(
        starting: NSColor(calibratedRed: 0.16, green: 0.18, blue: 0.24, alpha: 1),
        ending: NSColor(calibratedRed: 0.08, green: 0.09, blue: 0.13, alpha: 1))!
        .draw(in: shape, angle: -90)
    let c = NSPoint(x: s / 2, y: s * 0.40)
    let r = body.width * 0.32
    let w = body.width * 0.075
    let track = NSBezierPath()
    track.appendArc(withCenter: c, radius: r, startAngle: 180, endAngle: 0, clockwise: true)
    track.lineWidth = w
    track.lineCapStyle = .round
    NSColor(white: 1, alpha: 0.22).setStroke()
    track.stroke()
    let share: CGFloat = 0.72
    let fill = NSBezierPath()
    fill.appendArc(withCenter: c, radius: r, startAngle: 180, endAngle: 180 - 180 * share, clockwise: true)
    fill.lineWidth = w
    fill.lineCapStyle = .round
    NSColor(calibratedRed: 1.0, green: 0.62, blue: 0.18, alpha: 1).setStroke()
    fill.stroke()
    let a = (180 - 180 * share) * .pi / 180
    let needle = NSBezierPath()
    needle.move(to: c)
    needle.line(to: NSPoint(x: c.x + cos(a) * r * 0.78, y: c.y + sin(a) * r * 0.78))
    needle.lineWidth = w * 0.55
    needle.lineCapStyle = .round
    NSColor.white.setStroke()
    needle.stroke()
    NSColor.white.setFill()
    NSBezierPath(ovalIn: NSRect(x: c.x - w * 0.7, y: c.y - w * 0.7, width: w * 1.4, height: w * 1.4)).fill()
    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])!
}

for pt in [16, 32, 128, 256, 512] {
    try draw(pt).write(to: set.appendingPathComponent("icon_\(pt)x\(pt).png"))
    try draw(pt * 2).write(to: set.appendingPathComponent("icon_\(pt)x\(pt)@2x.png"))
}
let p = Process()
p.executableURL = URL(fileURLWithPath: "/usr/bin/iconutil")
p.arguments = ["-c", "icns", set.path, "-o", out.path]
try p.run()
p.waitUntilExit()
exit(p.terminationStatus)
