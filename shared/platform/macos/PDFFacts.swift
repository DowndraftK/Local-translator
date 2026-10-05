// Narrow native facts only: no page plan, persistence, translation or UI policy.
import Foundation
import PDFKit
import ImageIO
import UniformTypeIdentifiers
import CryptoKit

let version = "pdfkit-text-render-v1"
func hash(_ data: Data) -> String { SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined() }
func failure(_ message: String) -> NSError { NSError(domain: version, code: 1, userInfo: [NSLocalizedDescriptionKey: message]) }
func render(_ page: PDFPage, to path: String) throws -> [String: Any] {
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
    guard let image = context.makeImage(), let destination = CGImageDestinationCreateWithURL(URL(fileURLWithPath:path) as CFURL,UTType.png.identifier as CFString,1,nil) else { throw failure("渲染输出失败") }
    CGImageDestinationAddImage(destination,image,nil)
    guard CGImageDestinationFinalize(destination) else { throw failure("PNG输出未完成") }
    let bytes = try Data(contentsOf: URL(fileURLWithPath:path))
    return ["pixelWidth":width,"pixelHeight":height,"rotation":Int(rotation),"cropBox":[crop.minX,crop.minY,crop.width,crop.height],
            "scale":scale,"orientation":"up; PDF rotation applied; no deskew", "byteCount":bytes.count,"sha256":hash(bytes),
            "objectsReleasedAtProcessExit":true]
}

do {
    guard CommandLine.arguments.count == 3 else { throw failure("需要受控请求及结果路径") }
    let requestURL = URL(fileURLWithPath:CommandLine.arguments[1]), resultURL = URL(fileURLWithPath:CommandLine.arguments[2])
    let data = try Data(contentsOf:requestURL)
    guard data.count <= 65536, let req = try JSONSerialization.jsonObject(with:data) as? [String:Any],
          req["schema"] as? Int == 1, let action=req["action"] as? String, let request=req["request"] as? String else { throw failure("事实请求无效") }
    let os = ProcessInfo.processInfo.operatingSystemVersion
    let backend = "\(version);macOS=\(os.majorVersion).\(os.minorVersion).\(os.patchVersion)"
    var reply: [String:Any] = ["schema":1,"request":request,"action":action,"backend":backend,"complete":true]
    let start=Date()
    if action != "probe" {
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
                reply["image"]=try render(page,to:image)
            } else { throw failure("未实现的事实操作") }
        }
    }
    reply["elapsedSeconds"] = Date().timeIntervalSince(start)
    let result = try JSONSerialization.data(withJSONObject:reply,options:[.sortedKeys,.withoutEscapingSlashes])
    try result.write(to:resultURL,options:.atomic)
} catch {
    FileHandle.standardError.write(Data(error.localizedDescription.utf8))
    exit(2)
}
