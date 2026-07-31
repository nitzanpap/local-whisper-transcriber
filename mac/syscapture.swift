// The computer's own audio, without asking anyone to install a driver.
//
// macOS has no system-audio *input* device, which is why this project used to
// send people to BlackHole. A Core Audio process tap hands the same audio over
// directly: no kernel driver, no password, no reboot.
//
// It used to be ScreenCaptureKit, which works and costs the wrong permission.
// ScreenCaptureKit is a screen API: it can only ever ask for "Screen & System
// Audio Recording", it will not run without a video stream, and so the helper
// configured a two-pixel display capture at a frame a second and threw every
// frame away purely to keep audio arriving. Asking somebody for their screen in
// order to transcribe a meeting is a large thing to ask for a thing we do not
// want. A process tap asks for "System Audio Recording Only", which is the
// permission this actually needs, and takes no video at all.
//
// It writes raw signed 16-bit little-endian mono at 48 kHz to the path given,
// and nothing else. Raw rather than a WAV because the reader is ffmpeg, which is
// told the format on its own command line, and because every prefix of a raw
// stream is valid audio: a recording cut short is still a recording. Mono
// because the filter graph on the other side flattens each source to mono
// anyway, so carrying two channels this far would only be thrown away.
//
// Usage:
//   syscapture <path>   capture until SIGINT/SIGTERM, or until the reader goes
//
// There is no permission probe, because Core Audio offers no way to ask without
// asking: creating a tap succeeds whether or not the grant exists, and an
// ungranted one simply delivers silence. So the permission is requested at the
// moment of use, the way a normal application asks, and silence is reported
// afterwards by the level check that already runs on every recording.
//
// Exit codes matter to the caller: 3 means the audio machinery refused outright,
// which is a sentence for the user rather than a bug, and record.py says so.

import AVFoundation
import AudioToolbox
import CoreAudio
import Darwin

let DENIED: Int32 = 3
let OUT_RATE = 48000.0

func address(_ selector: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: selector,
                               mScope: kAudioObjectPropertyScopeGlobal,
                               mElement: kAudioObjectPropertyElementMain)
}

func fail(_ message: String, _ code: Int32 = 1) -> Never {
    FileHandle.standardError.write(Data("syscapture: \(message)\n".utf8))
    exit(code)
}

/// The UID of whatever the machine is playing through right now. The tap has to
/// hang off a real output device, and that is the one carrying the meeting.
func defaultOutputUID() -> String? {
    var addr = address(kAudioHardwarePropertyDefaultOutputDevice)
    var device = AudioDeviceID(0)
    var size = UInt32(MemoryLayout<AudioDeviceID>.size)
    guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject),
                                     &addr, 0, nil, &size, &device) == noErr,
          device != kAudioObjectUnknown else { return nil }
    var uidAddr = address(kAudioDevicePropertyDeviceUID)
    var uid: CFString = "" as CFString
    size = UInt32(MemoryLayout<CFString>.size)
    let status = withUnsafeMutablePointer(to: &uid) {
        AudioObjectGetPropertyData(device, &uidAddr, 0, nil, &size, $0)
    }
    return status == noErr ? uid as String : nil
}

/// What the tap is actually hearing, said out loud once a second.
///
/// Two questions look identical from outside this process and are not the same at
/// all: a machine playing nothing, and a tap that has not been allowed. A tap on an
/// idle output device delivers no callbacks whatever, so silence there is simply a
/// quiet room. A tap that is refused still receives callbacks once something plays
/// — full of digital zeros. So frames arriving with nothing in them is the shape of
/// a refusal, and frames not arriving at all is the shape of an ordinary silence.
/// Nobody outside can tell those apart, which is why this reports both.
final class Meter {
    private let lock = NSLock()
    private var frames = 0
    private var peak: Float = 0

    func add(_ samples: UnsafeBufferPointer<Int16>) {
        lock.lock()
        defer { lock.unlock() }
        frames += samples.count
        for v in samples { peak = max(peak, abs(Float(v) / 32767)) }
    }

    /// Prints and resets. Digital silence is reported as -120, which no real signal
    /// reaches, so the far end can tell "nothing at all" from "quiet".
    func report() {
        lock.lock()
        let (n, loudest) = (frames, peak)
        frames = 0
        peak = 0
        lock.unlock()
        guard n > 0 else { return }   // no callbacks: the machine is playing nothing
        let db = loudest > 0 ? 20 * log10(loudest) : -120
        FileHandle.standardError.write(Data(String(format: "syscapture: level %.1f frames %d\n",
                                                   db, n).utf8))
    }
}

/// Writes mono 16-bit samples to the far end, and knows when the far end has gone.
final class Sink {
    private let fd: Int32
    private let lock = NSLock()
    private(set) var gone = false

    init(fd: Int32) { self.fd = fd }

    func write(_ samples: UnsafeBufferPointer<Int16>) {
        // Serialised because Core Audio may deliver on more than one thread, and
        // two half-written frames interleaved would be audible as noise.
        lock.lock()
        defer { lock.unlock() }
        guard var p = UnsafeRawPointer(samples.baseAddress) else { return }
        var left = samples.count * MemoryLayout<Int16>.size
        while left > 0 {
            // write(2) rather than FileHandle.write, which raises an Objective-C
            // exception on a broken pipe that Swift cannot catch — the process
            // simply dies. And a broken pipe is the normal end of every recording:
            // ffmpeg reaches its -t limit or is stopped, and stops reading.
            let n = Darwin.write(fd, p, left)
            if n > 0 {
                p += n
                left -= n
                continue
            }
            // EINTR is worth retrying; anything else means the reader is gone and
            // there is nothing useful left to do with these samples.
            if n < 0 && errno == EINTR { continue }
            gone = true
            return
        }
    }
}

let args = Array(CommandLine.arguments.dropFirst())
guard let target = args.first, !target.isEmpty else {
    fail("no output path given", 2)
}

// EPIPE would otherwise kill the process before the write can be ignored.
signal(SIGPIPE, SIG_IGN)

// O_CREAT because the usual target is a plain file that does not exist yet;
// without it this failed with ENOENT on every recording the app made and worked
// only in the one place a FIFO had already been created. A FIFO still works:
// opening one for writing blocks until the reader arrives, which is the handshake
// that keeps the two ends from racing.
let fd = open(target, O_WRONLY | O_CREAT | O_TRUNC, 0o644)
if fd < 0 { fail("cannot write to \(target)", 2) }
let sink = Sink(fd: fd)
let meter = Meter()

guard let outputUID = defaultOutputUID() else {
    fail("no default output device to tap", 2)
}

// A global tap: everything the machine is playing. Nothing is excluded, which is
// what the ScreenCaptureKit version amounted to as well — it excluded the helper
// process, and the helper has never made a sound.
// ponytail: if the app's own playback ever needs excluding, translate the parent
// pid with kAudioHardwarePropertyTranslatePIDToProcessObject and pass it here.
let tapDescription = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
tapDescription.uuid = UUID()
tapDescription.name = "Local Whisper Transcriber"
tapDescription.isPrivate = true          // never shown in Audio MIDI Setup
tapDescription.muteBehavior = .unmuted   // the meeting must still come out of the speakers

var tap = AudioObjectID(kAudioObjectUnknown)
if AudioHardwareCreateProcessTap(tapDescription, &tap) != noErr || tap == kAudioObjectUnknown {
    fail("macOS would not create an audio tap", DENIED)
}

// What the tap will hand over: the device's own rate and channel count, in float.
// Read rather than assumed — a Mac playing at 44.1 kHz would otherwise be written
// out as though it were 48, which is a recording that plays back too fast and a
// transcript with every timestamp wrong.
var formatAddr = address(kAudioTapPropertyFormat)
var asbd = AudioStreamBasicDescription()
var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
guard AudioObjectGetPropertyData(tap, &formatAddr, 0, nil, &size, &asbd) == noErr,
      let tapFormat = AVAudioFormat(streamDescription: &asbd) else {
    AudioHardwareDestroyProcessTap(tap)
    fail("the audio tap would not say what format it produces")
}

// An aggregate device is how a tap is listened to. Private, so it never appears
// as a device anybody could pick by accident.
let aggregateUID = UUID().uuidString
let aggregate: [String: Any] = [
    kAudioAggregateDeviceNameKey: "Local Whisper Transcriber",
    kAudioAggregateDeviceUIDKey: aggregateUID,
    kAudioAggregateDeviceMainSubDeviceKey: outputUID,
    kAudioAggregateDeviceIsPrivateKey: true,
    kAudioAggregateDeviceIsStackedKey: false,
    kAudioAggregateDeviceTapAutoStartKey: true,
    kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: outputUID]],
    kAudioAggregateDeviceTapListKey: [[
        kAudioSubTapDriftCompensationKey: true,
        kAudioSubTapUIDKey: tapDescription.uuid.uuidString,
    ]],
]
var device = AudioObjectID(kAudioObjectUnknown)
if AudioHardwareCreateAggregateDevice(aggregate as CFDictionary, &device) != noErr {
    AudioHardwareDestroyProcessTap(tap)
    fail("macOS would not open a device to listen to the tap", DENIED)
}

func tearDown(_ proc: AudioDeviceIOProcID?) {
    if let proc {
        AudioDeviceStop(device, proc)
        AudioDeviceDestroyIOProcID(device, proc)
    }
    AudioHardwareDestroyAggregateDevice(device)
    AudioHardwareDestroyProcessTap(tap)
}

// Whatever the device runs at, in whatever layout, arrives here as 48 kHz mono
// 16-bit because that is what ffmpeg has been told on its own command line.
guard let outFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: OUT_RATE,
                                    channels: 1, interleaved: true),
      let converter = AVAudioConverter(from: tapFormat, to: outFormat) else {
    tearDown(nil)
    fail("cannot convert \(tapFormat) to 48 kHz mono")
}

var ioProc: AudioDeviceIOProcID?
let status = AudioDeviceCreateIOProcIDWithBlock(&ioProc, device, nil) { _, input, _, _, _ in
    guard !sink.gone else { return }
    let incoming = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input))
    guard let first = incoming.first, first.mDataByteSize > 0 else { return }
    let frames = AVAudioFrameCount(first.mDataByteSize / (tapFormat.streamDescription.pointee.mBytesPerFrame))
    guard frames > 0,
          let source = AVAudioPCMBuffer(pcmFormat: tapFormat, bufferListNoCopy: input),
          let out = AVAudioPCMBuffer(pcmFormat: outFormat,
                                     frameCapacity: AVAudioFrameCount(Double(frames) * OUT_RATE
                                                                      / tapFormat.sampleRate) + 16)
    else { return }

    var handed = false
    var error: NSError?
    converter.convert(to: out, error: &error) { _, outStatus in
        if handed {
            outStatus.pointee = .noDataNow
            return nil
        }
        handed = true
        outStatus.pointee = .haveData
        return source
    }
    guard error == nil, out.frameLength > 0, let channel = out.int16ChannelData else { return }
    let samples = UnsafeBufferPointer(start: channel[0], count: Int(out.frameLength))
    meter.add(samples)
    sink.write(samples)
}
if status != noErr || ioProc == nil {
    tearDown(nil)
    fail("macOS would not start reading the tap")
}

if AudioDeviceStart(device, ioProc) != noErr {
    tearDown(ioProc)
    fail("macOS would not start the audio device", DENIED)
}

// Once a second, whatever it is hearing. Its own queue: this must keep reporting
// while the main thread is parked waiting to be stopped.
let reporting = DispatchQueue(label: "syscapture.meter")
let ticker = DispatchSource.makeTimerSource(queue: reporting)
ticker.schedule(deadline: .now() + 1, repeating: 1)
ticker.setEventHandler { meter.report() }
ticker.resume()

// Stopping is the normal end of a recording, so it exits 0: whatever reached the
// far end before the signal is the recording. Signal sources have to outlive the
// scope that made them or they stop firing.
let queue = DispatchQueue(label: "syscapture.signal")
var sources: [DispatchSourceSignal] = []
let stop = DispatchSemaphore(value: 0)
for sig in [SIGINT, SIGTERM] {
    signal(sig, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: sig, queue: queue)
    source.setEventHandler { stop.signal() }
    source.resume()
    sources.append(source)
}
// The reader going away ends it too, and nothing else would notice. On its own
// queue: this loop never returns until it is over, and parked on the same serial
// queue as the signal sources it starved them — the helper then ignored SIGINT and
// SIGTERM alike and had to be killed.
DispatchQueue.global(qos: .utility).async {
    while !sink.gone { Thread.sleep(forTimeInterval: 0.25) }
    stop.signal()
}
stop.wait()

tearDown(ioProc)
close(fd)
exit(0)
