import Foundation
import Vision
import AppKit
import CoreGraphics

// OCR a scanned PDF (or an image) and print what Vision reads, rebuilt into
// lines: observations sharing a baseline are one row, ordered left to right.
// A metrics table is only useful if "Total denied" and its number stay on the
// same line. Pages are rasterised at 3x so table digits survive.
let SCALE: CGFloat = 3.0

func recognise(_ cg: CGImage) {
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.usesLanguageCorrection = false
    let handler = VNImageRequestHandler(cgImage: cg, options: [:])
    do { try handler.perform([req]) } catch { return }
    guard let obs = req.results else { return }

    var items: [(y: CGFloat, x: CGFloat, s: String)] = []
    for o in obs {
        guard let c = o.topCandidates(1).first else { continue }
        items.append((o.boundingBox.midY, o.boundingBox.minX, c.string))
    }
    items.sort { $0.y == $1.y ? $0.x < $1.x : $0.y > $1.y }

    var line: [(y: CGFloat, x: CGFloat, s: String)] = []
    func flush() {
        guard !line.isEmpty else { return }
        print(line.sorted { $0.x < $1.x }.map { $0.s }.joined(separator: " "))
        line = []
    }
    for it in items {
        if let first = line.first, abs(first.y - it.y) > 0.006 { flush() }
        line.append(it)
    }
    flush()
}

func render(_ page: CGPDFPage) -> CGImage? {
    let box = page.getBoxRect(.mediaBox)
    let w = Int(box.width * SCALE), h = Int(box.height * SCALE)
    guard w > 0, h > 0,
          let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8,
                              bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(),
                              bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)
    else { return nil }
    ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
    ctx.fill(CGRect(x: 0, y: 0, width: CGFloat(w), height: CGFloat(h)))
    ctx.scaleBy(x: SCALE, y: SCALE)
    ctx.translateBy(x: -box.origin.x, y: -box.origin.y)
    ctx.drawPDFPage(page)
    return ctx.makeImage()
}

for path in CommandLine.arguments.dropFirst() {
    if path.lowercased().hasSuffix(".pdf") {
        guard let doc = CGPDFDocument(URL(fileURLWithPath: path) as CFURL) else {
            FileHandle.standardError.write("cannot open \(path)\n".data(using: .utf8)!)
            continue
        }
        for i in 1...max(doc.numberOfPages, 1) {
            guard let page = doc.page(at: i), let img = render(page) else { continue }
            recognise(img)
        }
    } else if let img = NSImage(contentsOfFile: path),
              let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) {
        recognise(cg)
    }
}
