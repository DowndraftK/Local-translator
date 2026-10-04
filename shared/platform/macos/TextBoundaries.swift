import Foundation
import NaturalLanguage

// Platform facts only. The shared Python core owns packing and task rules.
let data = FileHandle.standardInput.readDataToEndOfFile()
guard data.count <= 1_048_576, let text = String(data: data, encoding: .utf8) else {
    fputs("Invalid or oversized UTF-8 input\n", stderr); exit(2)
}
let tokenizer = NLTokenizer(unit: .sentence)
tokenizer.string = text
var sentenceEnds = Set<String.Index>()
tokenizer.enumerateTokens(in: text.startIndex..<text.endIndex) { range, _ in
    sentenceEnds.insert(range.upperBound); return true
}
var rows: [[Int]] = [], offset = 0, index = text.startIndex
while index < text.endIndex {
    let character = text[index]
    index = text.index(after: index)
    offset += String(character).utf8.count
    rows.append([offset, character.isNewline ? 1 : 0, character.isWhitespace ? 1 : 0,
                 sentenceEnds.contains(index) ? 1 : 0])
}
let result = try JSONSerialization.data(withJSONObject: ["schema": 1, "byteCount": data.count, "boundaries": rows], options: [.sortedKeys])
FileHandle.standardOutput.write(result)
