import AppKit
import CoreText
import ImageIO
import UniformTypeIdentifiers

let destination = URL(fileURLWithPath: CommandLine.arguments.dropFirst().first ?? "artifacts")
try FileManager.default.createDirectory(at: destination, withIntermediateDirectories: true)
func draw(_ context: CGContext) {
    context.setFillColor(CGColor(gray: 1, alpha: 1))
    context.fill(CGRect(x: 0, y: 0, width: 1000, height: 600))
    let rows = ["Course Requirements", "Submit the application by September 30.", "The deposit is $250. The course is worth 3 credits.", "Late submissions will not be accepted."]
    for (i, row) in rows.enumerated() {
        context.textPosition = CGPoint(x: 50, y: 510 - i * 90)
        let line = CTLineCreateWithAttributedString(NSAttributedString(string: row, attributes: [.font: NSFont.systemFont(ofSize: 30), .foregroundColor: NSColor.black]))
        CTLineDraw(line, context)
    }
}
let context = CGContext(data: nil, width: 1000, height: 600, bitsPerComponent: 8, bytesPerRow: 4000, space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
draw(context)
let png = CGImageDestinationCreateWithURL(destination.appendingPathComponent("sample.png") as CFURL, UTType.png.identifier as CFString, 1, nil)!
CGImageDestinationAddImage(png, context.makeImage()!, nil)
guard CGImageDestinationFinalize(png) else { fatalError("PNG generation failed") }
var box = CGRect(x: 0, y: 0, width: 1000, height: 600)
let pdf = CGContext(destination.appendingPathComponent("sample.pdf") as CFURL, mediaBox: &box, nil)!
pdf.beginPDFPage(nil); draw(pdf); pdf.endPDFPage(); pdf.closePDF()
print("Synthetic PDF and PNG written to \(destination.path)")
