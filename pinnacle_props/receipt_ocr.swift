import Foundation
import Vision
import AppKit
let url = URL(fileURLWithPath: CommandLine.arguments[1])
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = false
try VNImageRequestHandler(url: url, options: [:]).perform([request])
// Vision returns columns in reading order; receipts need label/value rows.
let observations = (request.results ?? []).sorted { $0.boundingBox.midY > $1.boundingBox.midY }
var rows: [[VNRecognizedTextObservation]] = []
for observation in observations {
    if let last = rows.last, let first = last.first,
       abs(first.boundingBox.midY - observation.boundingBox.midY) < max(first.boundingBox.height, observation.boundingBox.height) * 0.6 {
        rows[rows.count - 1].append(observation)
    } else {
        rows.append([observation])
    }
}
let text = rows.map { row in
    row.sorted { $0.boundingBox.minX < $1.boundingBox.minX }
       .compactMap { $0.topCandidates(1).first?.string }.joined(separator: " ")
}.joined(separator: "\n")
print(text)
