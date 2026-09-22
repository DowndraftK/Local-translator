import AppKit
import Foundation

let destination = URL(fileURLWithPath: CommandLine.arguments[1])
try FileManager.default.createDirectory(at: destination, withIntermediateDirectories: true)
for (points, scale) in [(16,1), (16,2), (32,1), (32,2), (128,1), (128,2), (256,1), (256,2), (512,1), (512,2)] {
    let size = points * scale
    let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
    let factor = CGFloat(size) / 1024
    let transform = NSAffineTransform(); transform.scale(by: factor); transform.concat()
    let bounds = NSRect(x: 60, y: 60, width: 904, height: 904)
    let background = NSBezierPath(roundedRect: bounds, xRadius: 202, yRadius: 202)
    NSGradient(starting: NSColor(srgbRed: 0.07, green: 0.56, blue: 0.5, alpha: 1),
               ending: NSColor(srgbRed: 0.02, green: 0.29, blue: 0.30, alpha: 1))!.draw(in: background, angle: -70)
    let card = NSBezierPath(roundedRect: NSRect(x: 195, y: 226, width: 634, height: 572), xRadius: 78, yRadius: 78)
    NSColor.white.withAlphaComponent(0.12).setFill(); card.fill()
    let text = "译" as NSString
    let attributes: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 390, weight: .medium), .foregroundColor: NSColor.white]
    let extent = text.size(withAttributes: attributes)
    text.draw(at: NSPoint(x: (1024 - extent.width) / 2, y: (1024 - extent.height) / 2 + 32), withAttributes: attributes)
    let line = NSBezierPath(); line.lineWidth = 25; line.lineCapStyle = .round; line.lineJoinStyle = .round
    line.move(to: NSPoint(x: 336, y: 265)); line.line(to: NSPoint(x: 694, y: 265))
    line.move(to: NSPoint(x: 650, y: 309)); line.line(to: NSPoint(x: 694, y: 265)); line.line(to: NSPoint(x: 650, y: 221))
    NSColor.white.withAlphaComponent(0.88).setStroke(); line.stroke()
    NSGraphicsContext.restoreGraphicsState()
    let filename = "icon_\(points)x\(points)\(scale == 2 ? "@2x" : "").png"
    try bitmap.representation(using: .png, properties: [:])!.write(to: destination.appendingPathComponent(filename))
}
