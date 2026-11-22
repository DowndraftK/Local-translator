// Narrow native facts only: no page plan, persistence, translation or UI policy.
import Foundation
import PDFKit
import ImageIO
import UniformTypeIdentifiers
import CryptoKit
import Vision

let version = "pdfkit-text-render-v1"
func hash(_ data: Data) -> String { SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined() }
func failure(_ message: String) -> NSError { NSError(domain: version, code: 1, userInfo: [NSLocalizedDescriptionKey: message]) }
func render(_ page: PDFPage) throws -> (CGImage, [String: Any]) {
    guard let ref = page.pageRef else { throw failure("页面无法渲染") }
    let rotation = ((ref.rotationAngle % 360) + 360) % 360
    guard rotation % 90 == 0 else { throw failure("不支持非90度PDF旋转") }
    let crop = ref.getBoxRect(.cropBox).intersection(ref.getBoxRect(.mediaBox))
    guard !crop.isNull, crop.width.isFinite, crop.height.isFinite, crop.width > 0, crop.height > 0 else { throw failure("可见cropBox无效") }
    let sideways = rotation == 90 || rotation == 270
    let w = sideways ? crop.height : crop.width, h = sideways ? crop.width : crop.height
    let scale = min(3.0, 3000.0 / max(w,h), sqrt(9_000_000.0 / w / h))
    let width = max(1, Int(floor(w*scale))), height = max(1, Int(floor(h*scale)))
    guard width <= 3000, height <= 3000, width * height <= 9_000_000,
          let context = CGContext(data: nil,width: width,height: height,bitsPerComponent: 8,bytesPerRow: width*4,
              space: CGColorSpaceCreateDeviceRGB(),bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else { throw failure("无法分配有界位图") }
    let rect = CGRect(x:0,y:0,width:width,height:height)
    context.setFillColor(CGColor(gray:1,alpha:1)); context.fill(rect); context.clip(to:rect)
    context.scaleBy(x:Double(width)/w,y:Double(height)/h)
    context.concatenate(ref.getDrawingTransform(.cropBox,rect:CGRect(x:0,y:0,width:w,height:h),rotate:0,preserveAspectRatio:true))
    context.clip(to:crop); context.drawPDFPage(ref)
    guard let image = context.makeImage() else { throw failure("渲染输出失败") }
    return (image, ["pixelWidth":width,"pixelHeight":height,"rotation":Int(rotation),"cropBox":[crop.minX,crop.minY,crop.width,crop.height],
            "scale":scale,"orientation":"up; PDF rotation applied; no deskew", "objectsReleasedAtProcessExit":true])
}
func writePNG(_ image: CGImage, path: String, metadata: [String: Any]) throws -> [String: Any] {
    guard let destination = CGImageDestinationCreateWithURL(URL(fileURLWithPath:path) as CFURL,UTType.png.identifier as CFString,1,nil) else { throw failure("PNG输出失败") }
    CGImageDestinationAddImage(destination,image,nil)
    guard CGImageDestinationFinalize(destination) else { throw failure("PNG输出未完成") }
    let bytes = try Data(contentsOf: URL(fileURLWithPath:path))
    var value=metadata; value["byteCount"]=bytes.count;value["sha256"]=hash(bytes);return value
}
func recognizer(_ direction: String) -> VNRecognizeTextRequest {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = direction == "zh-en" ? ["zh-Hans", "en-US"] : ["en-US", "zh-Hans"]
    request.usesLanguageCorrection = true
    return request
}

do {
    guard CommandLine.arguments.count == 3 else { throw failure("需要受控请求及结果路径") }
    let requestURL = URL(fileURLWithPath:CommandLine.arguments[1]), resultURL = URL(fileURLWithPath:CommandLine.arguments[2])
    let data = try Data(contentsOf:requestURL)
    guard data.count <= 65536, let req = try JSONSerialization.jsonObject(with:data) as? [String:Any],
          req["schema"] as? Int == 1, let action=req["action"] as? String, let request=req["request"] as? String else { throw failure("事实请求无效") }
    let os = ProcessInfo.processInfo.operatingSystemVersion
    let pdfBackend = "\(version);macOS=\(os.majorVersion).\(os.minorVersion).\(os.patchVersion)"
    let isOCR = action == "ocr_probe" || action == "ocr"
    let direction = req["direction"] as? String ?? "en-zh"
    let vision = recognizer(direction)
    if let languages = req["recognitionLanguages"] as? [String] {
        guard !languages.isEmpty && languages.count <= 2 else { throw failure("识别语言无效") }
        vision.recognitionLanguages = languages
    } else if direction != "en-zh" && direction != "zh-en" {
        throw failure("方向无效")
    }
    let backend = isOCR ? "vision-accurate-v1;macOS=\(os.majorVersion).\(os.minorVersion).\(os.patchVersion);revision=\(vision.revision)" : pdfBackend
    var reply: [String:Any] = ["schema":1,"request":request,"action":action,"backend":backend,"complete":true]
    let start=Date()
    if action == "ocr_probe" {
        reply["availableLanguages"] = try vision.supportedRecognitionLanguages()
        reply["profile"] = ["engine":"Apple Vision", "revision":vision.revision,"recognitionLevel":"accurate",
            "usesLanguageCorrection":true,"pdfBackend":pdfBackend,"orientation":"up","autoDeskew":false,
            "resourceHashes":NSNull(),"resourceHashAvailability":"Apple system Vision language resources have no public per-language hash API",
            "license":"macOS system component; used locally, not redistributed"]
    } else if action != "probe" {
        guard let path=req["source"] as? String,let fingerprint=req["fingerprint"] as? String else { throw failure("来源字段缺失") }
        reply["fingerprint"] = fingerprint
        try autoreleasepool {
            guard let pdf=PDFDocument(url:URL(fileURLWithPath:path)) else { throw failure("PDF损坏或无法读取") }
            guard !pdf.isLocked else { throw failure("PDF已锁定；本预览不处理密码") }
            guard pdf.pageCount > 0 else { throw failure("PDF没有有效页") }
            if action == "metadata" { reply["totalPages"]=pdf.pageCount; return }
            guard let number=req["page"] as? Int,number>=1,number<=pdf.pageCount,let page=pdf.page(at:number-1) else { throw failure("物理页越界或损坏") }
            reply["page"]=number
            if let label=page.label { reply["label"]=label }
            if action == "page" {
                let text=page.string ?? ""
                guard text.utf8.count<=1_048_576 else { throw failure("单页文字超过1MiB保护") }
                reply["text"]=text
            } else if action == "render",let image=req["image"] as? String {
                let (bitmap,meta) = try render(page)
                reply["image"]=try writePNG(bitmap,path:image,metadata:meta)
            } else if action == "ocr" {
                let (bitmap,meta) = try render(page)
                guard Set(vision.recognitionLanguages).isSubset(of:Set(try vision.supportedRecognitionLanguages())) else { throw failure("所选方向的Vision语言资源不具备") }
                try VNImageRequestHandler(cgImage:bitmap,orientation:.up).perform([vision])
                var observations: [[String: Any]] = []
                for (nativeOrder,observation) in (vision.results ?? []).enumerated() {
                    guard let candidate=observation.topCandidates(1).first else { continue }
                    let r=observation.boundingBox
                    observations.append(["id":"page\(number)-\(observation.uuid)","order":observations.count,"nativeOrder":nativeOrder,
                        "text":candidate.string,"confidence":candidate.confidence,"rect":[r.minX,r.minY,r.width,r.height]])
                }
                guard observations.count<=4096 else { throw failure("OCR观察项超过单页4096项") }
                let text=observations.map {$0["text"] as! String}.joined(separator:"\n")
                guard text.utf8.count<=1_048_576 else { throw failure("OCR单页文字超过1MiB") }
                var raw=meta
                raw["backend"]=backend;raw["request"]=request;raw["observations"]=observations
                raw["languages"]=vision.recognitionLanguages;raw["coordinateSystem"]="normalized-bottom-left"
                raw["orientation"]="up";raw["textAngle"]=NSNull();raw["recognitionComplete"]=true
                raw["visionRevision"]=vision.revision;raw["usesLanguageCorrection"]=true
                reply["text"]=text;reply["ocr"]=raw
            } else { throw failure("未实现的事实操作") }
        }
    }
    reply["elapsedSeconds"] = Date().timeIntervalSince(start)
    let result = try JSONSerialization.data(withJSONObject:reply,options:[.sortedKeys,.withoutEscapingSlashes])
    guard result.count <= (isOCR ? 4_194_304 : 1_114_112) else { throw failure("事实结果超过有界容量") }
    try result.write(to:resultURL,options:.atomic)
} catch {
    FileHandle.standardError.write(Data(error.localizedDescription.utf8))
    exit(2)
}
