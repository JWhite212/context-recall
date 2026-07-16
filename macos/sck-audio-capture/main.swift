// sck-audio-capture — capture macOS system audio to a 16 kHz mono PCM-16 WAV.
//
// Usage:
//   sck-audio-capture --output <path.wav>   capture until SIGTERM/SIGINT, then finalise
//   sck-audio-capture --check-permission     print "granted" | "denied", exit 0
//
// While capturing, prints `rms=<float>` to stdout ~10x/sec for level metering.
// On Screen Recording denial / SCK failure, prints `error=...` to stderr and
// exits non-zero. Captures SYSTEM OUTPUT only (never the microphone) via the
// Screen Recording TCC service — the escape hatch for macOS builds where the
// Microphone service is broken.
import ScreenCaptureKit
import AVFoundation
import CoreMedia
import CoreGraphics
import Darwin

func fail(_ message: String) -> Never {
    FileHandle.standardError.write("error=\(message)\n".data(using: .utf8)!)
    exit(1)
}

final class SystemAudioCapturer: NSObject, SCStreamOutput, SCStreamDelegate {
    let audioQueue = DispatchQueue(label: "sck.audio")
    private let outURL: URL
    private let sampleRate: Double
    private let outFormat: AVAudioFormat
    private var file: AVAudioFile?
    private var converter: AVAudioConverter?
    private var lastEmit = Date(timeIntervalSince1970: 0)
    private var stopped = false

    init(outputPath: String, sampleRate: Double) {
        self.outURL = URL(fileURLWithPath: outputPath)
        self.sampleRate = sampleRate
        guard let outFormat = AVAudioFormat(
            commonFormat: .pcmFormatInt16,
            sampleRate: sampleRate,
            channels: 1,
            interleaved: true
        ) else {
            fail("invalid sample rate \(sampleRate)")
        }
        self.outFormat = outFormat
        super.init()
    }

    func openFile() throws {
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVSampleRateKey: sampleRate,
            AVNumberOfChannelsKey: 1,
            AVLinearPCMBitDepthKey: 16,
            AVLinearPCMIsFloatKey: false,
            AVLinearPCMIsBigEndianKey: false,
        ]
        self.file = try AVAudioFile(
            forWriting: outURL,
            settings: settings,
            commonFormat: .pcmFormatInt16,
            interleaved: true
        )
    }

    func finalizeAndExit() {
        // Runs on the main queue (signal handler). Serialize with the audio
        // queue so any in-flight write completes before we nil out `file` —
        // SCK does not guarantee the sample-handler queue has drained when
        // stopCapture() returns. Setting `stopped` here also stops the
        // handler from reopening (and truncating) the file on a late buffer.
        audioQueue.sync {
            stopped = true
            self.file = nil  // AVAudioFile finalises the WAV header on release.
        }
        exit(0)
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
                of type: SCStreamOutputType) {
        guard !stopped else { return }
        guard type == .audio, CMSampleBufferDataIsReady(sampleBuffer) else { return }
        guard let fmtDesc = CMSampleBufferGetFormatDescription(sampleBuffer),
              let asbdPtr = CMAudioFormatDescriptionGetStreamBasicDescription(fmtDesc) else { return }
        var asbd = asbdPtr.pointee
        guard let srcFormat = AVAudioFormat(streamDescription: &asbd) else { return }

        let numSamples = CMSampleBufferGetNumSamples(sampleBuffer)
        guard numSamples > 0,
              let srcBuffer = AVAudioPCMBuffer(
                  pcmFormat: srcFormat,
                  frameCapacity: AVAudioFrameCount(numSamples)) else { return }
        srcBuffer.frameLength = AVAudioFrameCount(numSamples)
        let copyStatus = CMSampleBufferCopyPCMDataIntoAudioBufferList(
            sampleBuffer, at: 0, frameCount: Int32(numSamples),
            into: srcBuffer.mutableAudioBufferList)
        guard copyStatus == noErr else { return }

        if converter == nil {
            converter = AVAudioConverter(from: srcFormat, to: outFormat)
        }
        guard let converter = converter else { return }

        let ratio = outFormat.sampleRate / srcFormat.sampleRate
        let outCapacity = AVAudioFrameCount(Double(numSamples) * ratio) + 32
        guard let outBuffer = AVAudioPCMBuffer(
            pcmFormat: outFormat, frameCapacity: outCapacity) else { return }

        var err: NSError?
        var provided = false
        converter.convert(to: outBuffer, error: &err) { _, statusPtr in
            if provided { statusPtr.pointee = .noDataNow; return nil }
            provided = true
            statusPtr.pointee = .haveData
            return srcBuffer
        }
        if err != nil || outBuffer.frameLength == 0 { return }

        if file == nil {
            do {
                try openFile()
            } catch {
                fail("failed to open output file: \(error)")
            }
        }
        do {
            try file?.write(from: outBuffer)
        } catch {
            fail("write failed: \(error)")
        }
        emitRMS(outBuffer)
    }

    private func emitRMS(_ buffer: AVAudioPCMBuffer) {
        let now = Date()
        guard now.timeIntervalSince(lastEmit) >= 0.1 else { return }
        lastEmit = now
        guard let ch = buffer.int16ChannelData else { return }
        let n = Int(buffer.frameLength)
        guard n > 0 else { return }
        var sumSq = 0.0
        for i in 0..<n {
            let v = Double(ch[0][i]) / 32768.0
            sumSq += v * v
        }
        let rms = (sumSq / Double(n)).squareRoot()
        print("rms=\(String(format: "%.6f", rms))")
        fflush(stdout)
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        fail("stream stopped: \(error)")
    }
}

func firstDisplayFilter() async throws -> SCContentFilter {
    let content = try await SCShareableContent.excludingDesktopWindows(
        false, onScreenWindowsOnly: true)
    guard let display = content.displays.first else { fail("no display available") }
    return SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])
}

func makeConfig(sampleRate: Int) -> SCStreamConfiguration {
    let config = SCStreamConfiguration()
    config.capturesAudio = true
    config.excludesCurrentProcessAudio = true   // don't capture our own output
    config.sampleRate = 48000                    // SCK native; we downsample to 16k
    config.channelCount = 2
    config.width = 2; config.height = 2          // SCK needs a video config even audio-only
    config.minimumFrameInterval = CMTime(value: 1, timescale: 1)
    return config
}

func runCapture(outputPath: String, sampleRate: Int) async {
    do {
        let filter = try await firstDisplayFilter()
        let capturer = SystemAudioCapturer(outputPath: outputPath, sampleRate: Double(sampleRate))
        let stream = SCStream(filter: filter, configuration: makeConfig(sampleRate: sampleRate),
                              delegate: capturer)
        try stream.addStreamOutput(capturer, type: .audio,
                                   sampleHandlerQueue: capturer.audioQueue)

        // Finalise cleanly on SIGTERM/SIGINT (the daemon stops us this way).
        for sig in [SIGTERM, SIGINT] { signal(sig, SIG_IGN) }
        for sig in [SIGTERM, SIGINT] {
            let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            src.setEventHandler {
                Task { try? await stream.stopCapture(); capturer.finalizeAndExit() }
            }
            src.resume()
            // Keep the source alive for the process lifetime.
            signalSources.append(src)
        }
        try await stream.startCapture()
        // Park forever; the signal handler exits the process.
        try await Task.sleep(nanoseconds: .max)
    } catch {
        fail("\(error)")
    }
}

var signalSources: [DispatchSourceSignal] = []

func checkPermission() {
    // CGPreflight returns true only when Screen Recording is granted.
    // It cannot distinguish "undetermined" from "denied", so report both as denied.
    print(CGPreflightScreenCaptureAccess() ? "granted" : "denied")
    exit(0)
}

// ---- Argument parsing ----
let args = CommandLine.arguments
if args.contains("--check-permission") {
    checkPermission()
}
guard let outIdx = args.firstIndex(of: "--output"), outIdx + 1 < args.count else {
    fail("usage: sck-audio-capture --output <path.wav> | --check-permission")
}
let outputPath = args[outIdx + 1]
var sampleRate = 16000
if let srIdx = args.firstIndex(of: "--sample-rate"), srIdx + 1 < args.count,
   let sr = Int(args[srIdx + 1]) {
    sampleRate = sr
}

Task { await runCapture(outputPath: outputPath, sampleRate: sampleRate) }
// runCapture only returns on error (success exits via signal handler).
RunLoop.main.run()
