import Foundation
import PDFKit
import Vision
import ImageIO

public enum DocumentExtractionMode: String, Codable, CaseIterable {
    case text, ocr
    public var label: String { self == .ocr ? "显式整页 OCR" : "PDF 文字层" }
}

public struct OCRObservation: Codable, Identifiable {
    public let id: String
    public let order: Int
    public let text: String
    public let confidence: Float
    /// Normalized coordinates in the rendered image, origin at bottom left.
    public let rect: [Double]
}
public struct OCRPageEvidence: Codable {
    public var observations: [OCRObservation]
    public var pixelWidth: Int
    public var pixelHeight: Int
    public var rotation: Int
    public var cropBox: [Double]
    public var languages = ["en-US", "zh-Hans"]
    public var coordinateSystem = "渲染图像左下角原点，归一化 [x,y,width,height]；图像 orientation=up，已应用 PDF rotation/cropBox"
    public var text: String { observations.map(\.text).joined(separator: "\n") }
}

/// The cancellation handle deliberately does not live on the PDF reader actor:
/// VNImageRequestHandler.perform is synchronous and would block an actor cancel message.
public final class OCRCancellation: @unchecked Sendable {
    private let lock = NSLock()
    private var cancelled = false
    private var request: VNRequest?
    public init() {}
    public var isCancelled: Bool { lock.lock(); defer { lock.unlock() }; return cancelled }
    public func cancel() {
        lock.lock(); cancelled = true; let current = request; lock.unlock()
        current?.cancel()
    }
    func install(_ value: VNRequest) throws {
        lock.lock()
        if cancelled { lock.unlock(); throw CancellationError() }
        request = value; lock.unlock()
    }
    func releaseRequest() { lock.lock(); request = nil; lock.unlock() }
    func check() throws { if isCancelled || Task.isCancelled { throw CancellationError() } }
}

public enum DocumentOCR {
    public static let maxEdge = 3000
    public static let maxPixels = 9_000_000
    public static func languages(direction: String) -> [String] { direction == "zh-en" ? ["zh-Hans", "en-US"] : ["en-US", "zh-Hans"] }
    public static let settings = "Vision accurate；语言优先级按所选翻译方向固定（英中 en-US/zh-Hans，中英 zh-Hans/en-US），不自动检测；语言校正开启；cropBox 可见区域；最长边 ≤3000 px、总像素 ≤9000000；缩放 ≤3；每次仅保留一页位图"
    public static let limitations = "OCR 已获得文字、人工核对与翻译完成是不同状态，均不证明 PDF 内容完整准确。置信度仅供核对；行顺序未经复杂排版修复。支持正向、清晰、简单单栏印刷体英/简中。整页 OCR 不混入文字层；重复及低置信度文字保留。"
    public static func pixelSize(width: Double, height: Double) throws -> (Int, Int) {
        guard width.isFinite, height.isFinite, width > 0, height > 0 else { throw M0Error.invalid("PDF 可见尺寸无效。") }
        let scale = min(3, Double(maxEdge) / max(width, height), sqrt(Double(maxPixels) / width / height))
        return (max(1, min(maxEdge, Int(floor(width * scale)))), max(1, min(maxEdge, Int(floor(height * scale)))))
    }
    static func render(_ page: PDFPage) throws -> (CGImage, Int32, CGRect) {
            guard let ref = page.pageRef else { throw M0Error.invalid("PDF 页没有可渲染内容。") }
            let rotation = ((ref.rotationAngle % 360) + 360) % 360
            guard rotation % 90 == 0 else { throw M0Error.invalid("不支持此页旋转角度；请先将原页转为正向。") }
            let crop = ref.getBoxRect(.cropBox).intersection(ref.getBoxRect(.mediaBox))
            let sideways = rotation == 90 || rotation == 270
            let (width, height) = try pixelSize(width: sideways ? crop.height : crop.width, height: sideways ? crop.width : crop.height)
            guard let context = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8,
                bytesPerRow: width * 4, space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else {
                throw M0Error.invalid("无法分配有界 PDF 位图。")
            }
            let target = CGRect(x: 0, y: 0, width: width, height: height)
            context.setFillColor(CGColor(gray: 1, alpha: 1)); context.fill(target)
            context.clip(to: target)
            // CGPDF's drawing transform does not upscale small pages. Scale the
            // bitmap context explicitly, then map rotation/crop in PDF points.
            let logicalWidth = sideways ? crop.height : crop.width
            let logicalHeight = sideways ? crop.width : crop.height
            context.scaleBy(x: Double(width) / logicalWidth, y: Double(height) / logicalHeight)
            context.concatenate(ref.getDrawingTransform(.cropBox,
                rect: CGRect(x: 0, y: 0, width: logicalWidth, height: logicalHeight), rotate: 0, preserveAspectRatio: true))
            context.clip(to: crop); context.drawPDFPage(ref)
            guard let image = context.makeImage() else { throw M0Error.invalid("PDF 渲染失败。") }
            return (image, rotation, crop)
    }
    // Called only from DocumentTextReader's serial actor, never from the main actor.
    static func recognize(_ page: PDFPage, number: Int, cancellation: OCRCancellation, direction: String) throws -> DocumentPageRecord {
        try cancellation.check()
        return try autoreleasepool {
            let (image, rotation, crop) = try render(page)
            try cancellation.check()
            let width = image.width, height = image.height
            let request = VNRecognizeTextRequest()
            request.recognitionLevel = .accurate
            request.recognitionLanguages = languages(direction: direction)
            request.usesLanguageCorrection = true
            try cancellation.install(request)
            defer { cancellation.releaseRequest() }
            try VNImageRequestHandler(cgImage: image, orientation: .up).perform([request])
            try cancellation.check() // Never accept partial results after cancel.
            let observations = (request.results ?? []).enumerated().compactMap { index, observation -> OCRObservation? in
                guard let candidate = observation.topCandidates(1).first else { return nil }
                let r = observation.boundingBox
                return OCRObservation(id: "page\(number)-\(observation.uuid)", order: index, text: candidate.string,
                    confidence: candidate.confidence, rect: [r.minX, r.minY, r.width, r.height])
            }
            let evidence = OCRPageEvidence(observations: observations, pixelWidth: width, pixelHeight: height,
                rotation: Int(rotation), cropBox: [crop.minX, crop.minY, crop.width, crop.height], languages: languages(direction: direction))
            var result = DocumentPageRecord(id: number, label: page.label, text: evidence.text,
                state: evidence.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? .noText : .extracted)
            result.mode = .ocr; result.ocr = evidence
            var issues = ["请对照原页核对漏字、顺序、数字与否定；置信度不是准确率。"]
            if observations.contains(where: { $0.confidence < 0.5 }) { issues.append("有低置信度观察项，已全部保留。") }
            if rotation != 0 { issues.append("已应用 PDF rotation=\(rotation)°；请确认文字正向，未自动纠偏。") }
            if let media = page.pageRef?.getBoxRect(.mediaBox), crop != media { issues.append("仅识别 cropBox 可见区域，裁切外内容未纳入。") }
            result.issue = issues.joined(separator: " ")
            return result
        }
    }
}
