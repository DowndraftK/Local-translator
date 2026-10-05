import Foundation
import Testing
@testable import TranslatorCore

private var b03Root: URL {
    URL(fileURLWithPath:#filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("fixtures/b03")
}

@Test func b03ProductionSwiftDocumentAndOCRSchemaOneFixtures() async throws {
    let output = ProcessInfo.processInfo.environment["B03_GENERATE_FIXTURES"].map { URL(fileURLWithPath:$0) }
    let root = output ?? b03Root
    let identities = ["33333333-4444-4555-8666-777777777777", "44444444-5555-4666-8777-888888888888"]
    if let output {
        try FileManager.default.createDirectory(at:output,withIntermediateDirectories:false)
        let sourcePath = try #require(ProcessInfo.processInfo.environment["B03_FIXTURE_SOURCE"])
        let data = try Data(contentsOf:URL(fileURLWithPath:sourcePath))
        let fingerprint = TaskRecoveryStore.hash(data)
        let recipe = TranslationConfiguration(model:"hy-mt2:1.8b-q8",direction:"en-zh",digest:"f5ee2ebab8a605d7790eee41a0a34bbc98b4b12792ad7cc9f216dd4949fd7ed1",profileVersion:1)
        for (index, identity) in identities.enumerated() {
            let id = UUID(uuidString:identity)!
            var p1=DocumentPageRecord(id:1,text:"Repeat 12. Repeat 12.\n",state:.extracted)
            var p2=DocumentPageRecord(id:2,state:.noText)
            var p3=DocumentPageRecord(id:3,text:"Final legacy page 3.\n",state:.extracted)
            p1.revision=UUID(uuidString:"11111111-0000-4000-8000-000000000001")!
            p2.revision=UUID(uuidString:"11111111-0000-4000-8000-000000000002")!
            p3.revision=UUID(uuidString:"11111111-0000-4000-8000-000000000003")!
            if index == 1 {
                p1.mode = .ocr; p2.mode = .ocr; p3.mode = .ocr
                p1.ocr=OCRPageEvidence(observations:[OCRObservation(id:"legacy-raw-1",order:0,text:"Repeat 12. Repeat 12.",confidence:0.75,rect:[0.1,0.8,0.5,0.04])],pixelWidth:1836,pixelHeight:2376,rotation:0,cropBox:[0,0,612,792])
                p3.ocr=OCRPageEvidence(observations:[OCRObservation(id:"legacy-raw-3",order:0,text:"Final legacy page 3.",confidence:0.55,rect:[0.1,0.8,0.5,0.04])],pixelWidth:1836,pixelHeight:2376,rotation:0,cropBox:[0,0,612,792])
                p3.edited = true
            }
            var doc=DocumentSnapshot(id:id,file:"legacy-source.pdf",fingerprint:fingerprint,originalPath:"/synthetic/legacy-source.pdf",totalPages:3,isPDF:true,range:try DocumentPageRange(first:1,last:3,total:3),model:recipe.model,direction:recipe.direction,pages:[p1,p2,p3],mode:index == 1 ? .ocr : .text)
            try doc.prepare(); doc.translationStarted=true; doc.phase="自编旧文档夹具，部分翻译"
            var job=TextTranslationJob(id:id,source:doc.sourceKey,model:recipe.model,direction:recipe.direction)
            job.segments=doc.segments; job.phase = .stopped
            var result=TranslationRecord(source:job.segments[0].requestText,translation:"旧文档成功前缀12。",model:recipe.model,direction:recipe.direction,elapsedSeconds:0.125,warnings:[])
            result.configurationBinding=recipe.binding; result.requestConfiguration=recipe
            job.segments[0].state = .completed; job.segments[0].result=result
            let asset=RecoveryAsset(originalPath:"/synthetic/legacy-source.pdf",filename:"source.pdf",fingerprint:fingerprint,byteCount:data.count)
            var task=RecoveryTask(id:id,revision:1,name:"B03 自编生产Swift旧格式夹具 \(index)",configuration:recipe,text:job,document:doc,drafts:index == 1 ? [3:"未应用旧OCR草稿456"] : [:],asset:asset)
            task.created=Date(timeIntervalSince1970:0);task.updated=task.created
            let store=TaskRecoveryStore(root:root.appendingPathComponent("legacy"));try await store.save(task,assetData:data)
            task.revision=2;task.updated=Date(timeIntervalSince1970:1);try await store.save(task)
            // Compact base64 fixture, materialized only into ignored/temp test roots.
            try JSONOutput.encode(["base64":data.base64EncodedString(),"sha256":fingerprint]).write(to:root.appendingPathComponent("source-bytes.json"))
            try FileManager.default.removeItem(at:root.appendingPathComponent("legacy/\(identity)/source.pdf"))
        }
    }
    for identity in identities {
        let dir=root.appendingPathComponent("legacy/\(identity)")
        let raw=try Data(contentsOf:dir.appendingPathComponent("current.json"))
        let envelope=try #require(JSONSerialization.jsonObject(with:raw) as? [String:Any])
        let encodedPayload = try #require(envelope["payload"] as? String)
        let payload=try #require(Data(base64Encoded:encodedPayload))
        let task=try JSONDecoder().decode(RecoveryTask.self,from:payload);try task.validate()
        #expect(task.configuration.contextVersion == nil && task.revision == 2)
        #expect(task.text?.segments[0].result?.translation == "旧文档成功前缀12。")
        #expect(task.document?.pages.map(\.id) == [1,2,3])
        #expect(try JSONOutput.encode(task.configuration) == Data(contentsOf:b03Root.deletingLastPathComponent().appendingPathComponent("b01/swift-recipe-v1-en-zh.json")))
    }
}
