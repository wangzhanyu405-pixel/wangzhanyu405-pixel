import Foundation
import Vision

for path in CommandLine.arguments.dropFirst() {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["en-US"]
    try VNImageRequestHandler(url: URL(fileURLWithPath: path)).perform([request])
    let rows: [[String: Any]] = (request.results ?? []).compactMap { observation in
        guard let text = observation.topCandidates(1).first?.string else { return nil }
        let box = observation.boundingBox
        return ["text": text, "x": box.minX, "r": box.maxX, "y": 1-box.midY]
    }
    let result = try JSONSerialization.data(withJSONObject: rows)
    print(String(data: result, encoding: .utf8)!)
}
