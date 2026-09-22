import AppKit
import Foundation
import PDFKit
import Vision
import ZIPFoundation

public enum DocumentImporter {
    public static func extract(_ url: URL, mode: String = "auto", pageLimit: Int = 10) async throws -> ImportedDocument {
        guard ["auto", "text", "ocr", "layout"].contains(mode), (1...200).contains(pageLimit) else {
            throw M0Error.invalid("mode 为 auto/text/ocr/layout；页数上限为 1–200。")
        }
        let size = try url.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0
        guard size <= 256_000_000 else { throw M0Error.invalid("M0 单文件上限为 256 MB。") }
        switch url.pathExtension.lowercased() {
        case "txt", "md":
            let text = try String(contentsOf: url, encoding: .utf8)
            let blocks = text.components(separatedBy: "\n\n").enumerated().compactMap { i, part -> TextBlock? in
                guard !part.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return nil }
                return TextBlock(id: "p\(i)", text: part, source: SourceRange(element: "paragraph:\(i + 1)"))
            }
            return ImportedDocument(file: url.lastPathComponent, method: "utf8-paragraphs", blocks: blocks,
                                    warnings: url.pathExtension.lowercased() == "md" ? ["M0 仅提取 Markdown 原文；尚未实现语法树及代码保护后的整篇翻译。"] : [])
        case "pdf": return try await pdf(url, mode: mode, pageLimit: pageLimit)
        case "docx", "pptx": return try OfficeImporter.extract(url)
        case "png", "jpg", "jpeg", "heic", "tif", "tiff":
            guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
                  let image = CGImageSourceCreateThumbnailAtIndex(source, 0, [
                    kCGImageSourceCreateThumbnailFromImageAlways: true,
                    kCGImageSourceThumbnailMaxPixelSize: 3500,
                    kCGImageSourceCreateThumbnailWithTransform: true
                  ] as CFDictionary) else { throw M0Error.invalid("无法解码图片。") }
            return try await imageDocument(image, name: url.lastPathComponent, mode: mode)
        default: throw M0Error.invalid("支持 PDF、DOCX、PPTX、TXT、MD 及常见图片。")
        }
    }

    private static func pdf(_ url: URL, mode: String, pageLimit: Int) async throws -> ImportedDocument {
        guard let document = PDFDocument(url: url) else { throw M0Error.invalid("无法读取 PDF。") }
        guard !document.isLocked else { throw M0Error.unavailable("PDF 已加密；M0 请先提供可读取的副本。") }
        var result = ImportedDocument(file: url.lastPathComponent, method: "pdf-\(mode)", blocks: [], warnings: [])
        if document.pageCount > pageLimit { result.warnings.append("仅处理前 \(pageLimit)/\(document.pageCount) 页；可显式调整 --pages。") }
        result.warnings.append("M0 不自动保证双栏阅读顺序及混合页完整性；请比较 text/ocr/layout 三种结果。")
        var evidence: [String] = []
        for pageIndex in 0..<min(document.pageCount, pageLimit) {
            try Task.checkCancellation()
            guard let page = document.page(at: pageIndex) else { continue }
            let nativeText = page.string ?? ""
            if mode == "text" || (mode == "auto" && !nativeText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty) {
                let bounds = page.bounds(for: .mediaBox)
                let lines = page.selection(for: bounds)?.selectionsByLine() ?? []
                for (i, line) in lines.enumerated() {
                    guard let text = line.string, !text.isEmpty else { continue }
                    let r = line.bounds(for: page)
                    result.blocks.append(TextBlock(id: "page\(pageIndex + 1)-line\(i)", text: text, kind: "line",
                        source: SourceRange(page: pageIndex + 1, element: "line:\(i)", rect: [r.minX, r.minY, r.width, r.height])))
                }
                if lines.isEmpty && !nativeText.isEmpty {
                    result.blocks.append(TextBlock(id: "page\(pageIndex + 1)", text: nativeText, source: SourceRange(page: pageIndex + 1)))
                }
                if nativeText.isEmpty { result.warnings.append("第 \(pageIndex + 1) 页无可提取文字。") }
            } else {
                let bounds = page.bounds(for: .mediaBox)
                let scale = min(3.0, 3000 / max(bounds.width, bounds.height))
                let preview = page.thumbnail(of: NSSize(width: bounds.width * scale, height: bounds.height * scale), for: .mediaBox)
                guard let cg = preview.cgImage(forProposedRect: nil, context: nil, hints: nil) else { throw M0Error.invalid("PDF 页渲染失败。") }
                var extracted = try await imageDocument(cg, name: "page\(pageIndex + 1)", mode: mode)
                for i in extracted.blocks.indices {
                    extracted.blocks[i].id = "page\(pageIndex + 1)-" + extracted.blocks[i].id
                    extracted.blocks[i].source.page = pageIndex + 1
                }
                result.blocks += extracted.blocks
                result.warnings += extracted.warnings.map { "第 \(pageIndex + 1) 页：\($0)" }
                if let json = extracted.layoutEvidenceJSON { evidence.append("{\"page\":\(pageIndex + 1),\"observations\":\(json)}") }
            }
        }
        if !evidence.isEmpty { result.layoutEvidenceJSON = "[" + evidence.joined(separator: ",") + "]" }
        return result
    }

    private static func imageDocument(_ image: CGImage, name: String, mode: String) async throws -> ImportedDocument {
        var result = ImportedDocument(file: name, method: mode == "layout" ? "vision-document" : "vision-text", blocks: [], warnings: [])
        if mode == "layout" {
            var request = RecognizeDocumentsRequest()
            request.textRecognitionOptions.recognitionLanguages = [Locale.Language(identifier: "en"), Locale.Language(identifier: "zh-Hans")]
            let observations = try await request.perform(on: image)
            result.layoutEvidenceJSON = String(decoding: try JSONOutput.encode(observations), as: UTF8.self)
            for (i, observation) in observations.enumerated() {
                result.blocks.append(TextBlock(id: "document\(i)", text: observation.document.text.transcript,
                    kind: "document", source: SourceRange(element: "layout-observation:\(i)")))
            }
            result.warnings.append("结构、表格及坐标保存在 layoutEvidenceJSON；M0 尚未将全部结构转换成最终应用的数据模型。")
        } else {
            let request = VNRecognizeTextRequest()
            request.recognitionLevel = .accurate
            request.recognitionLanguages = ["en-US", "zh-Hans"]
            request.usesLanguageCorrection = true
            try VNImageRequestHandler(cgImage: image).perform([request])
            for (i, observation) in (request.results ?? []).enumerated() {
                guard let candidate = observation.topCandidates(1).first else { continue }
                let r = observation.boundingBox
                result.blocks.append(TextBlock(id: "ocr\(i)", text: candidate.string, kind: "line",
                    source: SourceRange(element: "normalized-bottom-left", rect: [r.minX, r.minY, r.width, r.height])))
            }
            result.warnings.append("OCR 坐标为图像左下角原点的归一化坐标；文字行顺序未经复杂版面校正。")
        }
        if result.blocks.isEmpty { result.warnings.append("未识别出文字。") }
        return result
    }
}

final class XMLNode {
    let name: String
    let attributes: [String: String]
    var children: [XMLNode] = []
    var text = ""
    init(_ name: String, _ attributes: [String: String]) { self.name = name; self.attributes = attributes }
    func all(_ name: String) -> [XMLNode] { (self.name == name ? [self] : []) + children.flatMap { $0.all(name) } }
    func attribute(_ name: String) -> String? { attributes[name] ?? attributes.first { $0.key.split(separator: ":").last.map(String.init) == name }?.value }
}

final class SafeXML: NSObject, XMLParserDelegate {
    var root: XMLNode?
    var stack: [XMLNode] = []
    var error: Error?
    var count = 0
    static func parse(_ data: Data) throws -> XMLNode {
        // Reject DTDs in both UTF-8 and UTF-16 input before passing bytes to XMLParser.
        let utf8 = String(data: data, encoding: .utf8) ?? String(data: data, encoding: .utf16) ?? ""
        guard !utf8.uppercased().contains("<!DOCTYPE"), !utf8.uppercased().contains("<!ENTITY") else {
            throw M0Error.invalid("拒绝含 DTD/实体声明的 XML。")
        }
        let delegate = SafeXML(), parser = XMLParser(data: data)
        parser.shouldProcessNamespaces = true
        parser.shouldResolveExternalEntities = false
        parser.delegate = delegate
        guard parser.parse(), delegate.error == nil, let root = delegate.root else {
            throw delegate.error ?? parser.parserError ?? M0Error.invalid("XML 无效。")
        }
        return root
    }
    func parser(_ parser: XMLParser, didStartElement elementName: String, namespaceURI: String?, qualifiedName qName: String?, attributes attributeDict: [String: String]) {
        count += 1
        guard count < 150_000, stack.count < 100 else { error = M0Error.invalid("XML 结构超出 M0 上限。"); parser.abortParsing(); return }
        let node = XMLNode(elementName, attributeDict)
        if let parent = stack.last { parent.children.append(node) } else { root = node }
        stack.append(node)
    }
    func parser(_ parser: XMLParser, foundCharacters string: String) { stack.last?.text += string }
    func parser(_ parser: XMLParser, didEndElement elementName: String, namespaceURI: String?, qualifiedName qName: String?) { if !stack.isEmpty { stack.removeLast() } }
    func parser(_ parser: XMLParser, resolveExternalEntityName name: String, systemID: String?) -> Data? { nil }
    func parser(_ parser: XMLParser, foundInternalEntityDeclarationWithName name: String, value: String?) {
        error = M0Error.invalid("拒绝 XML 实体声明。"); parser.abortParsing()
    }
    func parser(_ parser: XMLParser, foundExternalEntityDeclarationWithName name: String, publicID: String?, systemID: String?) {
        error = M0Error.invalid("拒绝 XML 外部实体。"); parser.abortParsing()
    }
}

public enum OfficeImporter {
    public static func resolveRelationship(_ target: String, relativeTo directory: String = "ppt") throws -> String {
        guard !target.contains(":"), !target.contains("\\"), !target.hasPrefix("/") else { throw M0Error.invalid("拒绝外部或绝对路径关系。") }
        let path = (directory + "/" + target).split(separator: "/").reduce(into: [String]()) { parts, part in
            if part == ".." { if !parts.isEmpty { parts.removeLast() } else { parts.append("..") } }
            else if part != "." { parts.append(String(part)) }
        }.joined(separator: "/")
        guard path.hasPrefix("ppt/slides/"), path.hasSuffix(".xml"), !path.contains("..") else { throw M0Error.invalid("幻灯片关系超出 ppt/slides 范围。") }
        return path
    }

    public static func extract(_ url: URL) throws -> ImportedDocument {
        let archive = try Archive(url: url, accessMode: .read)
        var total: UInt64 = 0
        for entry in archive {
            total += UInt64(entry.uncompressedSize)
            guard total <= 512_000_000, !entry.path.hasPrefix("/"), !entry.path.split(separator: "/").contains(".."), entry.type != .symlink else {
                throw M0Error.invalid("Office 压缩包超过展开上限或含非法路径。")
            }
        }
        func read(_ path: String) throws -> XMLNode {
            guard let entry = archive[path], entry.uncompressedSize <= 16_000_000 else { throw M0Error.invalid("缺少或过大的 Office XML：\(path)") }
            var data = Data()
            _ = try archive.extract(entry) { chunk in
                guard data.count + chunk.count <= 16_000_000 else { throw M0Error.invalid("XML 超出读取上限。") }
                data.append(chunk)
            }
            return try SafeXML.parse(data)
        }
        var result = ImportedDocument(file: url.lastPathComponent, method: "ooxml-structural-probe", blocks: [], warnings: [])
        if url.pathExtension.lowercased() == "docx" {
            let root = try read("word/document.xml")
            func walk(_ node: XMLNode, path: String, inTable: Bool) {
                if node.name == "p" {
                    let text = node.all("t").map(\.text).joined()
                    if !text.isEmpty { result.blocks.append(TextBlock(id: path, text: text, kind: inTable ? "table-paragraph" : "paragraph", source: SourceRange(element: path))) }
                    return
                }
                for (i, child) in node.children.enumerated() { walk(child, path: "\(path)/\(child.name)[\(i)]", inTable: inTable || child.name == "tbl") }
            }
            walk(root, path: "word/document.xml", inTable: false)
            result.warnings.append("M0 提取正文和表格段落；列表编号、合并单元格、脚注、修订和文本框的最终语义尚未完整实现。")
        } else {
            let presentation = try read("ppt/presentation.xml")
            let relationships = try read("ppt/_rels/presentation.xml.rels")
            var targets: [String: String] = [:]
            for relation in relationships.all("Relationship") {
                if relation.attribute("TargetMode") == "External" { result.warnings.append("已忽略外部关系。"); continue }
                if let id = relation.attribute("Id"), let target = relation.attribute("Target") { targets[id] = target }
            }
            for (page, slide) in presentation.all("sldId").enumerated() {
                guard let rid = slide.attributes["r:id"] ?? slide.attributes.first(where: { $0.key.hasSuffix(":id") })?.value,
                      let target = targets[rid] else { throw M0Error.invalid("无法解析幻灯片顺序关系。") }
                let path = try resolveRelationship(target)
                let root = try read(path)
                for (i, paragraph) in root.all("p").enumerated() {
                    let text = paragraph.all("t").map(\.text).joined()
                    if !text.isEmpty { result.blocks.append(TextBlock(id: "slide\(page + 1)-p\(i)", text: text,
                        source: SourceRange(page: page + 1, element: "\(path)/paragraph[\(i)]"))) }
                }
            }
            result.warnings.append("M0 按 presentation.xml 关系读取页序；尚未还原形状坐标、母版继承、备注及表格语义。")
        }
        result.warnings.append("Office 图片 OCR、特殊对象和原始页面渲染未接入；不得将本探针视为完整首版文档支持。")
        if result.blocks.isEmpty { result.warnings.append("未提取出正文；请核对源文件。") }
        return result
    }
}
