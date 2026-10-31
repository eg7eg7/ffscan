// Apple Vision helper for scanned photos.
//
//   orient <image>...          one line per image: "<degrees clockwise>\t<faces>\t<path>"
//   orient --eyes <image>      one line per eye:   "<x>\t<y>\t<radius>" (fractions of image width/height,
//                                                   origin top-left; radius as a fraction of image width)
//
// Orientation: Vision finds faces at any rotation and reports each face's roll (in-plane tilt); the median
// roll, snapped to 90 degrees, is how far the photo is turned. No faces -> 0 (left as scanned).
// Tried and dropped: comparing face counts across orientations (detector is rotation-invariant) and
// text recognition (reads rotated text too, ~25x slower).
import Foundation
import ImageIO
import Vision

func load(_ path: String, maxSide: Int) -> CGImage? {
    guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil) else { return nil }
    return CGImageSourceCreateThumbnailAtIndex(src, 0, [
        kCGImageSourceCreateThumbnailFromImageAlways: true,
        kCGImageSourceThumbnailMaxPixelSize: maxSide,
    ] as CFDictionary)
}

func perform(_ request: VNRequest, on image: CGImage) {
    do { try VNImageRequestHandler(cgImage: image).perform([request]) } catch {
        FileHandle.standardError.write("vision error: \(error)\n".data(using: .utf8)!)
    }
}

var args = Array(CommandLine.arguments.dropFirst())

if args.first == "--scene" {
    // "<degrees>\t<s0>\t<s90>\t<s180>\t<s270>": per rotation, the summed confidence of the top-5 labels of
    // Vision's on-device scene classifier. Classifiers are usually most confident on upright images.
    for path in args.dropFirst() {
        guard let image = load(path, maxSide: 512) else { continue }
        var scores: [Double] = []
        for orientation in [CGImagePropertyOrientation.up, .right, .down, .left] {
            let request = VNClassifyImageRequest()
            do { try VNImageRequestHandler(cgImage: image, orientation: orientation).perform([request]) } catch {}
            let top = (request.results ?? []).sorted { $0.confidence > $1.confidence }.prefix(5)
            scores.append(top.reduce(0.0) { $0 + Double($1.confidence) })
        }
        let best = scores.indices.max { scores[$0] < scores[$1] }!
        print("\(best * 90)\t" + scores.map { String(format: "%.3f", $0) }.joined(separator: "\t"))
    }
    exit(0)
}

if args.first == "--text" {
    // Text on the back of a print (handwriting, stamps), read on-device: one recognised line per output line.
    // The back may be scanned in any orientation, so all four are tried and the most confident reading wins.
    for path in args.dropFirst() {
        guard let image = load(path, maxSide: 2000) else { continue }
        var best: (score: Float, lines: [String]) = (0, [])
        for orientation in [CGImagePropertyOrientation.up, .right, .down, .left] {
            let request = VNRecognizeTextRequest()
            request.recognitionLevel = .accurate
            request.automaticallyDetectsLanguage = true
            do { try VNImageRequestHandler(cgImage: image, orientation: orientation).perform([request]) } catch {}
            let reads = (request.results ?? []).compactMap { $0.topCandidates(1).first }.filter { $0.confidence >= 0.4 }
            let score = reads.reduce(Float(0)) { $0 + $1.confidence * Float($1.string.count) }
            if score > best.score { best = (score, reads.map(\.string)) }
        }
        print(best.lines.joined(separator: "\n"))
    }
    exit(0)
}

if args.first == "--eyes" {
    for path in args.dropFirst() {
        guard let image = load(path, maxSide: 3000) else { continue }
        let landmarks = VNDetectFaceLandmarksRequest()
        perform(landmarks, on: image)
        for face in landmarks.results ?? [] where face.confidence > 0.5 {
            for eye in [face.landmarks?.leftEye, face.landmarks?.rightEye].compactMap({ $0 }) {
                // normalizedPoints are relative to the face box; map them to the whole image
                let pts = eye.normalizedPoints.map { p in
                    CGPoint(x: face.boundingBox.minX + p.x * face.boundingBox.width,
                            y: face.boundingBox.minY + p.y * face.boundingBox.height)
                }
                guard let minX = pts.map(\.x).min(), let maxX = pts.map(\.x).max(),
                      let minY = pts.map(\.y).min(), let maxY = pts.map(\.y).max() else { continue }
                let cx = (minX + maxX) / 2, cy = 1 - (minY + maxY) / 2  // Vision origin is bottom-left
                print(String(format: "%.5f\t%.5f\t%.5f", cx, cy, (maxX - minX) / 2))
            }
        }
    }
    exit(0)
}

for path in args {
    guard let image = load(path, maxSide: 1024) else {
        print("0\t0\t\(path)")
        continue
    }
    let faces = VNDetectFaceRectanglesRequest()
    perform(faces, on: image)
    let rolls = (faces.results ?? []).filter { $0.confidence > 0.5 }.compactMap { $0.roll?.doubleValue }.sorted()
    var degrees = 0
    if !rolls.isEmpty {
        let roll = rolls[rolls.count / 2] * 180 / .pi  // median, degrees
        degrees = (Int((roll / 90).rounded()) * 90 % 360 + 360) % 360
    }
    print("\(degrees)\t\(rolls.count)\t\(path)")
}
