// Direct image facts only. Task ownership, persistence and scheduling stay in Python.
import Foundation
import ImageIO
import UniformTypeIdentifiers
import CryptoKit
import Vision

func failure(_ text: String) -> NSError { NSError(domain:"image-facts-v1",code:1,userInfo:[NSLocalizedDescriptionKey:text]) }
func hash(_ data: Data) -> String { SHA256.hash(data:data).map {String(format:"%02x",$0)}.joined() }
func recognizer(_ direction: String) -> VNRecognizeTextRequest {
    let request=VNRecognizeTextRequest();request.recognitionLevel = .accurate
    request.recognitionLanguages = direction == "zh-en" ? ["zh-Hans","en-US"] : ["en-US","zh-Hans"]
    request.usesLanguageCorrection=true;return request
}
func source(_ path: String, fingerprint: String) throws -> (CGImageSource, [String:Any]) {
    let url=URL(fileURLWithPath:path)
    let attr=try FileManager.default.attributesOfItem(atPath:path)
    guard let count=attr[.size] as? NSNumber,count.intValue>0,count.intValue<=67_108_864 else {throw failure("图片超过64MiB或为空")}
    let bytes=try Data(contentsOf:url,options:.mappedIfSafe)
    guard bytes.count==count.intValue,hash(bytes)==fingerprint else {throw failure("封存图片hash不匹配")}
    guard let input=CGImageSourceCreateWithData(bytes as CFData,nil),CGImageSourceGetCount(input)==1,
          CGImageSourceGetStatus(input) == .statusComplete,
          CGImageSourceGetStatusAtIndex(input,0) == .statusComplete,
          let type=CGImageSourceGetType(input) as String?,
          let properties=CGImageSourceCopyPropertiesAtIndex(input,0,nil) as? [String:Any],
          let w=properties[kCGImagePropertyPixelWidth as String] as? Int,
          let h=properties[kCGImagePropertyPixelHeight as String] as? Int,
          w>0,h>0,w<=40_000_000,h<=40_000_000,w*h<=40_000_000 else {throw failure("损坏、截断、多图或超过4000万像素")}
    let format: String
    if type==UTType.png.identifier {
        format="PNG"
        guard bytes.suffix(12)==Data([0,0,0,0,73,69,78,68,174,66,96,130]) else {throw failure("PNG未完整结束")}
        if let png=properties[kCGImagePropertyPNGDictionary as String] as? [String:Any],png["LoopCount"] != nil {throw failure("不支持动画PNG")}
    } else if type==UTType.jpeg.identifier {
        format="JPEG"
        guard bytes.suffix(2)==Data([255,217]) else {throw failure("JPEG未完整结束")}
    } else {throw failure("只支持实际PNG/JPEG编码")}
    // Force a full bounded decode once, so a metadata-only success cannot accept
    // a broken/truncated source. This disposable process releases it at exit.
    guard CGImageSourceCreateImageAtIndex(input,0,[kCGImageSourceShouldCacheImmediately:true] as CFDictionary) != nil else {throw failure("图片完整解码失败")}
    let orientation=properties[kCGImagePropertyOrientation as String] as? Int ?? 1
    guard (1...8).contains(orientation) else {throw failure("图片方向无效")}
    let swap=orientation>=5
    return (input,["contract":"image-facts-v1","format":format,"sourceWidth":w,"sourceHeight":h,
        "exifOrientation":orientation,"displayWidth":swap ? h:w,"displayHeight":swap ? w:h,
        "hasAlpha":properties[kCGImagePropertyHasAlpha as String] as? Bool ?? false,"frameCount":1,
        "orientationApplied":true,"autoDeskew":false,"alphaBackground":"white"])
}
func raster(_ input: CGImageSource, geometry: [String:Any], edge: Int) throws -> (CGImage,[String:Any]) {
    guard let thumb=CGImageSourceCreateThumbnailAtIndex(input,0,[kCGImageSourceCreateThumbnailFromImageAlways:true,
        kCGImageSourceCreateThumbnailWithTransform:true,kCGImageSourceThumbnailMaxPixelSize:edge,
        kCGImageSourceShouldCacheImmediately:true] as CFDictionary) else {throw failure("无法生成有界方向栅格")}
    let w=thumb.width,h=thumb.height
    guard w>0,h>0,w<=edge,h<=edge,w*h<=9_000_000,
          let context=CGContext(data:nil,width:w,height:h,bitsPerComponent:8,bytesPerRow:w*4,
            space:CGColorSpaceCreateDeviceRGB(),bitmapInfo:CGImageAlphaInfo.noneSkipLast.rawValue) else {throw failure("有界栅格分配失败")}
    context.setFillColor(CGColor(gray:1,alpha:1));context.fill(CGRect(x:0,y:0,width:w,height:h))
    context.draw(thumb,in:CGRect(x:0,y:0,width:w,height:h))
    guard let bitmap=context.makeImage() else {throw failure("栅格输出失败")}
    let dw=geometry["displayWidth"] as! Int,dh=geometry["displayHeight"] as! Int
    return (bitmap,["pixelWidth":w,"pixelHeight":h,"scale":min(Double(w)/Double(dw),Double(h)/Double(dh)),
        "displayToRaster":[Double(w)/Double(dw),Double(h)/Double(dh)],"orientation":"up","alphaBackground":"white",
        "autoDeskew":false,"objectsReleasedAtProcessExit":true])
}

do {
    guard CommandLine.arguments.count==3 else {throw failure("需要受控请求和结果路径")}
    let data=try Data(contentsOf:URL(fileURLWithPath:CommandLine.arguments[1]))
    guard data.count<=65536,let req=try JSONSerialization.jsonObject(with:data) as? [String:Any],
          req["schema"] as? Int==1,let action=req["action"] as? String,let request=req["request"] as? String else {throw failure("事实请求无效")}
    let direction=req["direction"] as? String ?? "en-zh"
    let vision=recognizer(direction),os=ProcessInfo.processInfo.operatingSystemVersion
    if let languages = req["recognitionLanguages"] as? [String] {
        guard !languages.isEmpty && languages.count <= 2 else { throw failure("识别语言无效") }
        vision.recognitionLanguages = languages
    } else if direction != "en-zh" && direction != "zh-en" {
        throw failure("方向无效")
    }
    let backend="vision-image-accurate-v1;macOS=\(os.majorVersion).\(os.minorVersion).\(os.patchVersion);revision=\(vision.revision)"
    let start=Date()
    var reply:[String:Any]=["schema":1,"contract":"image-facts-v1","request":request,"action":action,"backend":backend,"complete":true]
    if action=="image_probe" {
        reply["availableLanguages"]=try vision.supportedRecognitionLanguages()
        reply["profile"]=["engine":"Apple Vision","revision":vision.revision,"recognitionLevel":"accurate",
            "usesLanguageCorrection":true,"imageDecode":"ImageIO","orientation":"EXIF applied before recognition",
            "autoDeskew":false,"alphaBackground":"white","resourceHashes":NSNull(),
            "resourceHashAvailability":"Apple system resources have no public per-language hash API",
            "license":"macOS system component; used locally, not redistributed"]
    } else {
        guard let path=req["source"] as? String,let fingerprint=req["fingerprint"] as? String else {throw failure("缺少图片来源")}
        let (input,geometry)=try source(path,fingerprint:fingerprint)
        reply["fingerprint"]=fingerprint;reply["geometry"]=geometry
        if action=="image_metadata" {} else {
            guard let sourceID=req["sourceID"] as? String,UUID(uuidString:sourceID) != nil else {throw failure("图片来源UUID无效")}
            reply["sourceID"]=sourceID
            if action=="image_render",let path=req["image"] as? String,let edge=req["maxEdge"] as? Int,edge==2400 {
                let (bitmap,meta)=try raster(input,geometry:geometry,edge:edge)
                guard let dest=CGImageDestinationCreateWithURL(URL(fileURLWithPath:path) as CFURL,UTType.png.identifier as CFString,1,nil) else {throw failure("PNG输出失败")}
                CGImageDestinationAddImage(dest,bitmap,nil)
                guard CGImageDestinationFinalize(dest) else {throw failure("PNG输出未完成")}
                let bytes=try Data(contentsOf:URL(fileURLWithPath:path));var image=meta
                image["byteCount"]=bytes.count;image["sha256"]=hash(bytes);reply["image"]=image
            } else if action=="image_ocr" {
                let (bitmap,meta)=try raster(input,geometry:geometry,edge:3000)
                guard Set(vision.recognitionLanguages).isSubset(of:Set(try vision.supportedRecognitionLanguages())) else {throw failure("离线识别语言不可用")}
                try VNImageRequestHandler(cgImage:bitmap,orientation:.up).perform([vision])
                var observations:[[String:Any]]=[]
                for (nativeOrder,o) in (vision.results ?? []).enumerated() {
                    guard let candidate=o.topCandidates(1).first else {continue}
                    let r=o.boundingBox
                    observations.append(["id":"image-\(o.uuid)","order":observations.count,"nativeOrder":nativeOrder,
                        "text":candidate.string,"confidence":candidate.confidence,"rect":[r.minX,r.minY,r.width,r.height]])
                }
                guard observations.count<=4096 else {throw failure("识别观察项超过4096项")}
                let text=observations.map {$0["text"] as! String}.joined(separator:"\n")
                guard text.utf8.count<=1_048_576 else {throw failure("识别文字超过1MiB")}
                var raw=meta
                raw["contract"]="image-ocr-v1";raw["backend"]=backend;raw["request"]=request
                raw["sourceID"]=sourceID;raw["sourceFingerprint"]=fingerprint;raw["sourceGeometry"]=geometry
                raw["languages"]=vision.recognitionLanguages;raw["coordinateSystem"]="normalized-bottom-left"
                raw["textAngle"]=NSNull();raw["recognitionComplete"]=true;raw["visionRevision"]=vision.revision
                raw["usesLanguageCorrection"]=true;raw["observations"]=observations
                reply["unit"]=1;reply["text"]=text;reply["ocr"]=raw
            } else {throw failure("不支持的图片事实操作")}
        }
    }
    reply["elapsedSeconds"]=Date().timeIntervalSince(start)
    let result=try JSONSerialization.data(withJSONObject:reply,options:[.sortedKeys,.withoutEscapingSlashes])
    guard result.count<=4_194_304 else {throw failure("事实结果超过4MiB")}
    try result.write(to:URL(fileURLWithPath:CommandLine.arguments[2]),options:.atomic)
} catch {FileHandle.standardError.write(Data(error.localizedDescription.utf8));exit(2)}
