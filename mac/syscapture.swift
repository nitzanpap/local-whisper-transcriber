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
    private let side: String
    private var frames = 0
    private var peak: Float = 0

    init(_ side: String) { self.side = side }

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
        FileHandle.standardError.write(Data(String(format: "syscapture: %@ level %.1f frames %d\n",
                                                   side, db, n).utf8))
    }
}

/// A clock that keeps counting while the machine is asleep.
///
/// On Darwin CLOCK_MONOTONIC does that and CLOCK_UPTIME_RAW does not, and a sleep
/// is precisely the gap this file exists to fill: a clock that stops when the Mac
/// does would measure a 31-second nap as no time passing at all.
func rightNow() -> UInt64 { clock_gettime_nsec_np(CLOCK_MONOTONIC) }

/// Writes mono 16-bit samples to the far end, and knows when the far end has gone.
///
/// It also keeps the file as long as the recording has been running, which is not
/// the same thing as writing down what arrived. A Core Audio tap on an output
/// device that is playing nothing delivers no callbacks whatever — measured, six
/// seconds of a quiet machine produce zero bytes — so a file made only of what
/// arrived is a file with every silence cut out of it. That is not a smaller
/// recording, it is a wrong one: every word after a pause carries a timestamp
/// earlier than the moment it was said, and the two channels drift apart from each
/// other. Measured once at 31 seconds missing from a 70-second recording.
final class Sink {
    private let fd: Int32
    private let lock = NSLock()
    private(set) var gone = false
    private var started: UInt64 = 0
    private var written = 0                                    // frames, padding included
    private var padded = 0.0                                   // seconds since sound last arrived
    private let silence = [Int16](repeating: 0, count: 4800)   // a tenth of a second

    init(fd: Int32) { self.fd = fd }

    /// Second zero of the recording. Everything written is measured from here.
    func begin() {
        lock.lock()
        defer { lock.unlock() }
        started = rightNow()
    }

    /// The samples the tap just handed over, after whatever silence it did not.
    func write(_ samples: UnsafeBufferPointer<Int16>) {
        // Serialised because Core Audio may deliver on more than one thread, and
        // two half-written frames interleaved would be audible as noise.
        lock.lock()
        // Whatever arrives before the clock starts is thrown away rather than
        // written without a place in time. Both sides are started together and
        // given second zero together, so both discard the same moment and neither
        // ends up ahead of the other.
        guard started != 0 else { lock.unlock(); return }
        catchUp()
        put(samples)
        written += samples.count
        // Said once, when sound comes back, rather than once a second while it is
        // away: the app keeps the last 120 lines of this, and a meeting full of
        // ordinary pauses would otherwise push every other message out of it.
        let quiet = padded
        padded = 0
        lock.unlock()
        if quiet >= 2 {
            FileHandle.standardError.write(
                Data(String(format: "syscapture: nothing played for %.1fs; that silence is in "
                            + "the recording rather than cut out of it\n", quiet).utf8))
        }
    }

    /// Called on a timer too, so a long silence is written down as it passes rather
    /// than all at once when sound returns. That keeps the size of the file an
    /// honest clock, which is what the app reads to say how long a recording has run.
    func idle() {
        lock.lock()
        defer { lock.unlock() }
        catchUp()
    }

    /// Silence for the time between what has been written and what has elapsed.
    /// Caller holds the lock.
    private func catchUp() {
        guard started != 0 else { return }
        let elapsed = Double(rightNow() - started) / 1e9
        let short = Int((elapsed * OUT_RATE).rounded()) - written
        guard short > 0 else { return }
        var left = short
        while left > 0 && !gone {
            let n = min(left, silence.count)
            silence.withUnsafeBufferPointer { put(UnsafeBufferPointer(rebasing: $0[0..<n])) }
            left -= n
        }
        written += short - left
        padded += Double(short - left) / OUT_RATE
    }

    /// Caller holds the lock.
    private func put(_ samples: UnsafeBufferPointer<Int16>) {
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

// --- input devices -----------------------------------------------------------

/// Every microphone this machine has, as (uid, name, isDefault).
///
/// By UID rather than by index. ffmpeg's device list is positional, and positions
/// move when anything is plugged in or taken away — a stored `1` has already meant
/// two different microphones on this machine. A UID belongs to the device.
func inputDevices() -> [(uid: String, name: String, isDefault: Bool)] {
    var addr = address(kAudioHardwarePropertyDevices)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject),
                                         &addr, 0, nil, &size) == noErr else { return [] }
    var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
    guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject),
                                     &addr, 0, nil, &size, &ids) == noErr else { return [] }

    var defaultAddr = address(kAudioHardwarePropertyDefaultInputDevice)
    var fallback = AudioDeviceID(0)
    var one = UInt32(MemoryLayout<AudioDeviceID>.size)
    _ = AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject),
                                   &defaultAddr, 0, nil, &one, &fallback)

    return ids.compactMap { id in
        // Something with no input streams is an output, and not a microphone.
        var streams = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyStreams,
            mScope: kAudioObjectPropertyScopeInput,
            mElement: kAudioObjectPropertyElementMain)
        var bytes: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(id, &streams, 0, nil, &bytes) == noErr,
              bytes > 0 else { return nil }
        guard let uid = stringProperty(id, kAudioDevicePropertyDeviceUID),
              let name = stringProperty(id, kAudioObjectPropertyName) else { return nil }
        return (uid, name, id == fallback)
    }
}

func stringProperty(_ id: AudioDeviceID, _ selector: AudioObjectPropertySelector) -> String? {
    var addr = address(selector)
    var value: CFString = "" as CFString
    var size = UInt32(MemoryLayout<CFString>.size)
    let status = withUnsafeMutablePointer(to: &value) {
        AudioObjectGetPropertyData(id, &addr, 0, nil, &size, $0)
    }
    return status == noErr ? value as String : nil
}

func inputDevice(uid: String?) -> AudioDeviceID? {
    var addr = address(kAudioHardwarePropertyDevices)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject),
                                         &addr, 0, nil, &size) == noErr else { return nil }
    var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
    guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject),
                                     &addr, 0, nil, &size, &ids) == noErr else { return nil }
    if let uid, !uid.isEmpty {
        return ids.first { stringProperty($0, kAudioDevicePropertyDeviceUID) == uid }
    }
    var defaultAddr = address(kAudioHardwarePropertyDefaultInputDevice)
    var found = AudioDeviceID(0)
    var one = UInt32(MemoryLayout<AudioDeviceID>.size)
    guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject),
                                     &defaultAddr, 0, nil, &one, &found) == noErr,
          found != kAudioObjectUnknown else { return nil }
    return found
}

// --- what was asked for ------------------------------------------------------

var tapPath = ""
var micPath = ""
var micUID = ""
var listing = false

var pending = Array(CommandLine.arguments.dropFirst())
while let flag = pending.first {
    pending.removeFirst()
    switch flag {
    case "--list-inputs": listing = true
    case "--tap": tapPath = pending.isEmpty ? "" : pending.removeFirst()
    case "--mic": micPath = pending.isEmpty ? "" : pending.removeFirst()
    case "--mic-device": micUID = pending.isEmpty ? "" : pending.removeFirst()
    // The old shape, still meaningful: one bare path is where to put the tap.
    default: if tapPath.isEmpty && !flag.hasPrefix("--") { tapPath = flag }
    }
}

if listing {
    for device in inputDevices() {
        print("\(device.uid)\t\(device.name)\t\(device.isDefault ? "default" : "")")
    }
    exit(0)
}
if tapPath.isEmpty && micPath.isEmpty {
    fail("nothing to record: give --tap and/or --mic a path", 2)
}

// EPIPE would otherwise kill the process before the write can be ignored.
signal(SIGPIPE, SIG_IGN)

/// O_CREAT because the usual target is a plain file that does not exist yet;
/// without it this failed with ENOENT on every recording the app made and worked
/// only in the one place a FIFO had already been created. A FIFO still works:
/// opening one for writing blocks until the reader arrives, which is the handshake
/// that keeps the two ends from racing.
func openSink(_ path: String) -> Sink {
    let fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0o644)
    if fd < 0 { fail("cannot write to \(path)", 2) }
    return Sink(fd: fd)
}

let tapSink = tapPath.isEmpty ? nil : openSink(tapPath)
let micSink = micPath.isEmpty ? nil : openSink(micPath)
let meter = Meter("computer")
let micMeter = Meter("voice")

// --- the microphone ----------------------------------------------------------
//
// Taken through Core Audio rather than handed to ffmpeg, because ffmpeg's
// avfoundation input loses samples. Measured on the same microphone at the same
// moment: Core Audio delivered 47,509 frames a second against a nominal 48,000,
// which is the ratio you would expect from the moment before the first buffer
// arrives; ffmpeg's input delivered 0.86 of real time, steadily, and its own log
// accounted for every packet it was given with no decode errors. The samples were
// never reaching it. A recording made that way is an eighth short and drifts about
// seven minutes an hour away from the other side of the same conversation.
//
// Capturing both sides here also means one process, one clock and one moment when
// recording starts, so the two files line up by construction instead of being
// two programs started in sequence and hoping.

var micProc: AudioDeviceIOProcID?
var micDevice = AudioDeviceID(kAudioObjectUnknown)

func startMicrophone() -> Bool {
    guard let sink = micSink else { return true }
    guard let device = inputDevice(uid: micUID.isEmpty ? nil : micUID) else {
        fail("no microphone called \(micUID.isEmpty ? "the default input" : micUID)", 2)
    }
    micDevice = device
    var formatAddr = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyStreamFormat,
        mScope: kAudioObjectPropertyScopeInput,
        mElement: kAudioObjectPropertyElementMain)
    var asbd = AudioStreamBasicDescription()
    var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
    guard AudioObjectGetPropertyData(device, &formatAddr, 0, nil, &size, &asbd) == noErr,
          let deviceFormat = AVAudioFormat(streamDescription: &asbd),
          let out = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: OUT_RATE,
                                  channels: 1, interleaved: true),
          let converter = AVAudioConverter(from: deviceFormat, to: out) else {
        fail("the microphone would not say what format it produces", 2)
    }

    let status = AudioDeviceCreateIOProcIDWithBlock(&micProc, device, nil) { _, input, _, _, _ in
        guard !sink.gone else { return }
        let incoming = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input))
        guard let first = incoming.first, first.mDataByteSize > 0 else { return }
        let bytesPerFrame = deviceFormat.streamDescription.pointee.mBytesPerFrame
        guard bytesPerFrame > 0 else { return }
        let frames = AVAudioFrameCount(first.mDataByteSize / bytesPerFrame)
        guard frames > 0,
              let source = AVAudioPCMBuffer(pcmFormat: deviceFormat, bufferListNoCopy: input),
              let converted = AVAudioPCMBuffer(
                  pcmFormat: out,
                  frameCapacity: AVAudioFrameCount(Double(frames) * OUT_RATE
                                                   / deviceFormat.sampleRate) + 16)
        else { return }
        var handed = false
        var error: NSError?
        converter.convert(to: converted, error: &error) { _, outStatus in
            if handed { outStatus.pointee = .noDataNow; return nil }
            handed = true
            outStatus.pointee = .haveData
            return source
        }
        guard error == nil, converted.frameLength > 0,
              let channel = converted.int16ChannelData else { return }
        let samples = UnsafeBufferPointer(start: channel[0], count: Int(converted.frameLength))
        micMeter.add(samples)
        sink.write(samples)
    }
    if status != noErr || micProc == nil {
        fail("macOS would not start reading the microphone", DENIED)
    }
    if AudioDeviceStart(device, micProc) != noErr {
        fail("macOS would not start the microphone", DENIED)
    }
    return true
}

func stopMicrophone() {
    if let proc = micProc, micDevice != kAudioObjectUnknown {
        AudioDeviceStop(micDevice, proc)
        AudioDeviceDestroyIOProcID(micDevice, proc)
        micProc = nil
    }
}

// The microphone first, and the tap afterwards. Creating the aggregate device that
// carries a tap reconfigures the audio HAL, and a capture opened after that never
// delivers a sample — measured both ways round, twice, with permissions
// uninvolved. The order is the whole reason this is not two independent programs.
_ = startMicrophone()

if tapSink == nil {
    // A microphone on its own: no tap, no aggregate device, nothing else to build.
    let alone = DispatchSource.makeTimerSource(queue: DispatchQueue(label: "syscapture.mic"))
    alone.schedule(deadline: .now() + 0.1, repeating: 0.1)
    alone.setEventHandler {
        micMeter.report()
        micSink?.idle()
    }
    alone.resume()
    micSink?.begin()
    waitForStop(micSink)
    stopMicrophone()
    micSink?.idle()
    exit(0)
}
let sink = tapSink!

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
// Second zero, from the moment the device is actually running.
sink.begin()
/// Park until somebody stops us, or until the far end goes away.
///
/// Stopping is the normal end of a recording, so it exits 0: whatever reached the
/// far end before the signal is the recording.
func waitForStop(_ watching: Sink?) {
    // Signal sources have to outlive the scope that made them or they stop firing.
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
    // queue as the signal sources it starved them — the helper then ignored SIGINT
    // and SIGTERM alike and had to be killed.
    if let watching {
        DispatchQueue.global(qos: .utility).async {
            while !watching.gone { Thread.sleep(forTimeInterval: 0.25) }
            stop.signal()
        }
    }
    stop.wait()
    _ = sources
}

// Both meters and both sinks, on one timer. Ten times a second rather than once:
// a needle fed one number a second does not look like a slow needle, it looks like
// a broken one — and the level this reports has always been a peak over the window,
// so a shorter window is simply a truer picture of a voice rather than an average
// of one. Its own queue: this must keep reporting while the main thread is parked
// waiting to be stopped.
let reporting = DispatchQueue(label: "syscapture.meter")
let ticker = DispatchSource.makeTimerSource(queue: reporting)
ticker.schedule(deadline: .now() + 0.1, repeating: 0.1)
ticker.setEventHandler {
    meter.report()
    sink.idle()
    if let mic = micSink {
        micMeter.report()
        mic.idle()
    }
}
ticker.resume()

// Second zero, for both sides at the same instant. Everything either of them wrote
// before this moment was dropped, so neither begins ahead of the other — which is
// what replaces the 2.84 seconds the two captures used to be apart when they were
// two programs started one after the other.
sink.begin()
micSink?.begin()

waitForStop(sink)

tearDown(ioProc)
stopMicrophone()
// The last stretch of quiet counts as much as the first. Without this a meeting
// that ends on a pause comes out shorter than it was, and the other channel — which
// kept running until the same signal — is left hanging past the end of this one.
sink.idle()
micSink?.idle()
exit(0)
