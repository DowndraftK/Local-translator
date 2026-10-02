import Foundation

private final class NoRedirects: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
}

public struct OllamaModel: Codable {
    public var name: String
    public var size: Int64?
    public var digest: String?
    public var remote_host: String?
    public var remote_model: String?
}

public struct ChatChunk: Decodable {
    public struct Message: Decodable { public var content: String? }
    public var message: Message?
    public var done: Bool?
    public var done_reason: String?
    public var eval_count: Int?
    public var load_duration: Double?
    public var prompt_eval_count: Int?
    public var prompt_eval_duration: Double?
    public var eval_duration: Double?
    public var error: String?
}

public struct ChatAccumulator {
    public private(set) var text = ""
    public private(set) var finished = false
    public private(set) var outputTokens: Int?
    public private(set) var finalChunk: ChatChunk?
    public init() {}
    @discardableResult public mutating func consume(_ data: Data) throws -> String {
        guard !finished else { throw M0Error.invalid("完成标记后仍收到额外响应。") }
        let chunk = try JSONDecoder().decode(ChatChunk.self, from: data)
        if let error = chunk.error { throw M0Error.unavailable("Ollama：\(error)") }
        let delta = chunk.message?.content ?? ""
        guard text.utf8.count + delta.utf8.count <= 256_000 else { throw M0Error.incomplete("模型输出超过验证程序上限。") }
        text += delta
        if chunk.done == true {
            guard chunk.done_reason == "stop" else {
                throw M0Error.incomplete("生成未正常完成（\(chunk.done_reason ?? "缺少结束原因")），不保存为成功。")
            }
            guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
                throw M0Error.incomplete("模型返回了空译文。")
            }
            finished = true; outputTokens = chunk.eval_count; finalChunk = chunk
        }
        return delta
    }
    public func requireComplete() throws {
        guard finished else { throw M0Error.incomplete("响应中断，未收到正常完成标记。") }
    }
}

public final class OllamaEngine {
    public let baseURL: URL
    private let session: URLSession
    private let cancellationLock = NSLock()
    private var requestsCancelled = false
    public static func validateEndpoint(_ string: String) throws -> URL {
        guard let components = URLComponents(string: string),
              components.scheme == "http",
              ["127.0.0.1", "[::1]", "::1"].contains(components.host ?? ""),
              components.user == nil, components.password == nil,
              components.query == nil, components.fragment == nil,
              components.path.isEmpty || components.path == "/",
              let url = components.url else {
            throw M0Error.invalid("服务地址只接受 http://127.0.0.1:端口 或 http://[::1]:端口。")
        }
        return url
    }
    public init(endpoint: String = "http://127.0.0.1:11434") throws {
        baseURL = try Self.validateEndpoint(endpoint)
        let configuration = URLSessionConfiguration.ephemeral
        configuration.connectionProxyDictionary = [:]
        configuration.urlCache = nil
        configuration.timeoutIntervalForRequest = 120
        configuration.timeoutIntervalForResource = 600
        session = URLSession(configuration: configuration, delegate: NoRedirects(), delegateQueue: nil)
    }
    deinit { session.invalidateAndCancel() }
    /// Cancels only this engine instance's requests, never the shared Ollama process.
    public func cancelRequests() {
        cancellationLock.lock(); requestsCancelled = true; cancellationLock.unlock()
        // Invalidating here races with an in-flight async function creating its
        // next URLSessionTask, which raises an uncatchable NSException. Keep the
        // session valid until deinit, reject new requests, cancel existing tasks.
        session.getAllTasks { tasks in tasks.forEach { $0.cancel() } }
    }
    private func checkCancellation() throws {
        try Task.checkCancellation()
        cancellationLock.lock(); let cancelled = requestsCancelled; cancellationLock.unlock()
        if cancelled { throw CancellationError() }
    }

    private func request(_ path: String, body: [String: Any]? = nil) throws -> URLRequest {
        try checkCancellation()
        var r = URLRequest(url: baseURL.appendingPathComponent(path))
        if let body {
            r.httpMethod = "POST"
            r.setValue("application/json", forHTTPHeaderField: "Content-Type")
            r.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        return r
    }
    private func check(_ response: URLResponse) throws {
        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
            throw M0Error.unavailable("本地服务请求失败，HTTP \((response as? HTTPURLResponse)?.statusCode ?? 0)。不使用云端回退。")
        }
    }
    public func models() async throws -> [OllamaModel] {
        var r = try request("api/tags"); r.timeoutInterval = 5
        let (data, response) = try await session.data(for: r); try check(response)
        struct Models: Decodable { var models: [OllamaModel] }
        return try JSONDecoder().decode(Models.self, from: data).models
    }
    @discardableResult public func verifyLocalModel(_ name: String) async throws -> [String] {
        let installed = try await models()
        guard let selected = installed.first(where: { $0.name == name }) else {
            throw M0Error.unavailable("本机没有已列出的模型 \(name)。请先显式准备资源；程序不会自动拉取。")
        }
        guard selected.remote_host == nil, selected.remote_model == nil,
              !name.lowercased().contains("cloud") else { throw M0Error.invalid("拒绝云模型。") }
        let (data, response) = try await session.data(for: request("api/show", body: ["model": name]))
        try check(response)
        guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              object["remote_host"] == nil, object["remote_model"] == nil,
              let capabilities = object["capabilities"] as? [String], capabilities.contains("completion") else {
            throw M0Error.invalid("无法确认模型为支持本地文字生成的模型。")
        }
        return capabilities
    }
    public func translate(_ source: String, model: String, direction: String = "en-zh",
                          glossary: [GlossaryTerm] = [], configuration: TranslationConfiguration? = nil,
                          onText: ((String) -> Void)? = nil) async throws -> TranslationRecord {
        guard ["en-zh", "zh-en"].contains(direction) else { throw M0Error.invalid("方向应为 en-zh 或 zh-en。") }
        try Task.checkCancellation()
        var recipe = configuration ?? TranslationConfiguration(model: model, direction: direction)
        try recipe.validate()
        guard recipe.model == model, recipe.direction == direction, configuration == nil || glossary.isEmpty else {
            throw M0Error.invalid("请求与保存配置不匹配；不能复用旧结果。")
        }
        if !glossary.isEmpty {
            let terms = "参考下面的翻译：\n" + glossary.map { "\($0.source) 翻译成 \($0.target)" }.joined(separator: "\n") + "\n\n"
            recipe.userPrefix = terms + recipe.userPrefix
        }
        var messages = [["role": "user", "content": recipe.userPrefix + source]]
        if let system = recipe.systemPrompt { messages.insert(["role": "system", "content": system], at: 0) }
        let generation = recipe.options.mapValues { $0 as Any }
        let promptBytes = messages.reduce(0) { $0 + ($1["content"]?.utf8.count ?? 0) } - source.utf8.count
        try TranslationBudget.validate(source: source, promptBytes: promptBytes)
        let installed = try await models().first { $0.name == model }
        if let digest = recipe.modelDigest {
            guard installed?.digest == digest else {
                throw M0Error.unavailable("已安装模型与任务保存的 digest 不同或模型缺失；请恢复原模型，或新建任务。")
            }
        } else { recipe.modelDigest = installed?.digest }
        let capabilities = try await verifyLocalModel(model)
        try Task.checkCancellation()
        var body: [String: Any] = ["model": model, "stream": true, "keep_alive": recipe.keepAlive,
                                   "messages": messages, "options": generation]
        // Non-thinking models may reject the think parameter rather than ignoring it.
        if capabilities.contains("thinking"), recipe.disableThinking { body["think"] = false }
        let r = try request("api/chat", body: body)
        let start = Date()
        let (bytes, response) = try await session.bytes(for: r); try check(response)
        var accumulator = ChatAccumulator()
        var firstText: Double?
        for try await line in bytes.lines {
            try Task.checkCancellation()
            if line.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { continue }
            guard line.utf8.count <= 256_000 else { throw M0Error.incomplete("响应行过大。") }
            let delta = try accumulator.consume(Data(line.utf8))
            if !delta.isEmpty {
                if firstText == nil { firstText = Date().timeIntervalSince(start) }
                onText?(delta)
            }
            if accumulator.finished { break }
        }
        try Task.checkCancellation()
        try accumulator.requireComplete()
        var result = TranslationRecord(source: source, translation: accumulator.text, model: model,
                                 direction: direction, elapsedSeconds: Date().timeIntervalSince(start),
                                 firstTextSeconds: firstText, outputTokens: accumulator.outputTokens,
                                 warnings: ContentChecks.warnings(source: source, target: accumulator.text, glossary: glossary))
        result.configurationBinding = configuration?.binding ?? recipe.binding
        result.requestConfiguration = recipe
        result.promptProfile = recipe.promptProfile
        result.modelLoadSeconds = accumulator.finalChunk?.load_duration.map { $0 / 1_000_000_000 }
        result.promptTokens = accumulator.finalChunk?.prompt_eval_count
        result.promptEvaluationSeconds = accumulator.finalChunk?.prompt_eval_duration.map { $0 / 1_000_000_000 }
        result.generationSeconds = accumulator.finalChunk?.eval_duration.map { $0 / 1_000_000_000 }
        return result
    }
}
