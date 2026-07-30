// The computer's own audio, without asking anyone to install a driver.
//
// macOS has no system-audio *input* device, which is why this project used to
// send people to BlackHole. ScreenCaptureKit hands the same audio over directly:
// no kernel driver, no password, no reboot — one permission grant instead.
//
// It writes raw signed 16-bit little-endian mono at 48 kHz to the path given,
// and nothing else. Raw rather than a WAV because the reader is ffmpeg, which is
// told the format on its own command line, and because every prefix of a raw
// stream is valid audio: a recording cut short is still a recording. Mono
// because the filter graph on the other side flattens each source to mono
// anyway, so carrying two channels this far would only be thrown away.
//
// Usage:
//   syscapture --probe    print {"granted":bool} and exit, never prompting
//   syscapture --request  the same, but ask macOS to prompt if it never has
//   syscapture <path>     capture until SIGINT/SIGTERM, or until the reader goes
//
// Exit codes matter to the caller: 3 means the permission was refused, which is
// a sentence for the user rather than a bug, and record.py says so.

import AVFoundation
import CoreGraphics
import Darwin
import ScreenCaptureKit

let DENIED: Int32 = 3

/// Turns whatever ScreenCaptureKit produces into the one format ffmpeg is told to expect.
final class Writer: NSObject, SCStreamOutput {
    private let out: FileHandle
    private let lock = NSLock()

    init(out: FileHandle) {
        self.out = out
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio else { return }
        // Deinterleaved float is what SCStream gives; averaging the channels is
        // the downmix, and the clamp is what stops a sum louder than full scale
        // from wrapping around into a click.
        var mono: [Int16] = []
        try? sb.withAudioBufferList { list, _ in
            let bufs = Array(list)
            guard let first = bufs.first else { return }
            let frames = Int(first.mDataByteSize) / 4
            mono.reserveCapacity(frames)
            let channels = bufs.compactMap { $0.mData?.assumingMemoryBound(to: Float.self) }
            guard !channels.isEmpty else { return }
            for i in 0..<frames {
                var sum: Float = 0
                for c in channels { sum += c[i] }
                let v = max(-1.0, min(1.0, sum / Float(channels.count)))
                mono.append(Int16(v * 32767))
            }
        }
        guard !mono.isEmpty else { return }
        let data = mono.withUnsafeBufferPointer { Data(buffer: $0) }
        // Serialised because SCStream may deliver on more than one queue, and two
        // half-written frames interleaved would be audible as noise.
        lock.lock()
        defer { lock.unlock() }
        // A write that fails means the reader is gone — ffmpeg hit its -t limit or
        // was stopped. That is a finished recording, not an error to shout about.
        out.write(data)
    }
}

@main
struct SysCapture {
    static func main() async {
        let args = Array(CommandLine.arguments.dropFirst())

        // Asked before anything is offered in the UI, so it must never prompt:
        // preflight reports what is already granted and stays silent otherwise.
        if args.first == "--probe" {
            print("{\"granted\":\(CGPreflightScreenCaptureAccess())}")
            exit(0)
        }
        // Preflight cannot tell "refused" from "never asked", and refusing a user
        // who has simply never been asked would be a dead end. This is the one
        // place allowed to raise the system prompt.
        if args.first == "--request" {
            let granted = CGPreflightScreenCaptureAccess() || CGRequestScreenCaptureAccess()
            print("{\"granted\":\(granted)}")
            exit(0)
        }
        guard let target = args.first, !target.isEmpty else {
            FileHandle.standardError.write(Data("syscapture: no output path given\n".utf8))
            exit(2)
        }

        // EPIPE would otherwise kill the process before the write can be ignored.
        signal(SIGPIPE, SIG_IGN)

        // Opening a FIFO for writing blocks until ffmpeg opens it for reading, which
        // is the handshake that keeps the two ends from racing at startup.
        guard let out = FileHandle(forWritingAtPath: target) else {
            FileHandle.standardError.write(Data("syscapture: cannot write to \(target)\n".utf8))
            exit(2)
        }

        do {
            let content = try await SCShareableContent.excludingDesktopWindows(
                false, onScreenWindowsOnly: false)
            guard let display = content.displays.first else {
                FileHandle.standardError.write(Data("syscapture: no display to capture from\n".utf8))
                exit(2)
            }

            let cfg = SCStreamConfiguration()
            cfg.capturesAudio = true
            // Our own output would otherwise be captured too, and a transcript of
            // the app playing back its own recording is not worth having.
            cfg.excludesCurrentProcessAudio = true
            cfg.sampleRate = 48000
            cfg.channelCount = 2
            // Audio is the whole point; the video ScreenCaptureKit insists on
            // configuring is kept as small and as slow as it will go.
            cfg.width = 2
            cfg.height = 2
            cfg.minimumFrameInterval = CMTime(value: 1, timescale: 1)

            let filter = SCContentFilter(
                display: display, excludingApplications: [], exceptingWindows: [])
            let stream = SCStream(filter: filter, configuration: cfg, delegate: nil)
            let writer = Writer(out: out)
            try stream.addStreamOutput(writer, type: .audio, sampleHandlerQueue: .global(qos: .userInitiated))
            try await stream.startCapture()

            // Stopping is the normal end of a recording, so it exits 0: whatever
            // reached the far end before the signal is the recording.
            await withTaskGroup(of: Void.self) { group in
                group.addTask { await waitForSignal() }
                await group.next()
            }
            try? await stream.stopCapture()
            try? out.close()
            exit(0)
        } catch {
            let text = "\(error)"
            // -3801 is ScreenCaptureKit for "the user said no", which the caller
            // turns into instructions rather than a stack trace.
            let refused = text.contains("-3801") || text.lowercased().contains("declined")
            FileHandle.standardError.write(Data("syscapture: \(text)\n".utf8))
            exit(refused ? DENIED : 1)
        }
    }

    /// Resolves on SIGINT or SIGTERM, the two ways record.py asks a capture to stop.
    private static func waitForSignal() async {
        await withCheckedContinuation { (cont: CheckedContinuation<Void, Never>) in
            let queue = DispatchQueue(label: "syscapture.signal")
            var done = false
            let finish = {
                queue.async {
                    guard !done else { return }
                    done = true
                    cont.resume()
                }
            }
            for sig in [SIGINT, SIGTERM] {
                signal(sig, SIG_IGN)
                let src = DispatchSource.makeSignalSource(signal: sig, queue: queue)
                src.setEventHandler { finish() }
                src.resume()
                sources.append(src)
            }
        }
    }
}

// Signal sources must outlive the function that made them or they stop firing.
nonisolated(unsafe) var sources: [DispatchSourceSignal] = []
