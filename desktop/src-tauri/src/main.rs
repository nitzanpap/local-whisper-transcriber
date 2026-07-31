// A window and a menu bar item around the local backend.
//
// The app owns the backend's lifetime: it starts it on launch and kills it when it
// quits. Closing the window is not quitting — it hides it, and the app goes on
// recording with only the menu bar to show for it, which is the point of a thing
// that runs through a meeting. Quitting is a deliberate act: Quit in the menu,
// Cmd-Q, or the system asking.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{ErrorKind, Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::menu::{Menu, MenuEvent, MenuItem, PredefinedMenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};

const PORT: u16 = 8765;
const START_TIMEOUT: Duration = Duration::from_secs(90); // a cold `uv run` resolves deps

/// The backend we started, if we started one. None when we attached to a server
/// that was already running, which must not be killed on quit.
struct Backend(Mutex<Option<Child>>);

fn port_open() -> bool {
    TcpStream::connect(("127.0.0.1", PORT)).is_ok()
}

/// Finder launches apps with a minimal PATH that has no Homebrew in it, so `uv`
/// has to be found the same way the backend finds ffmpeg: by looking.
fn find_uv() -> Option<PathBuf> {
    let mut candidates: Vec<PathBuf> = vec![
        "/opt/homebrew/bin/uv".into(),
        "/usr/local/bin/uv".into(),
    ];
    if let Some(home) = std::env::var_os("HOME") {
        let home = PathBuf::from(home);
        candidates.push(home.join(".cargo/bin/uv"));
        candidates.push(home.join(".local/bin/uv"));
    }
    candidates.into_iter().find(|p| p.is_file())
}

fn start_backend(app: &tauri::App) -> Result<Option<Child>, String> {
    if port_open() {
        return Ok(None); // something is already serving; leave it alone
    }
    let uv = find_uv().ok_or(
        "uv was not found. Install it with `brew install uv`, then open this app again.",
    )?;
    let backend = app
        .path()
        .resolve("backend", tauri::path::BaseDirectory::Resource)
        .map_err(|e| format!("cannot locate the bundled backend: {e}"))?;

    let child = Command::new(uv)
        .args(["run", "--script", "app.py"])
        .current_dir(&backend)
        // So the backend can stop itself if this app is force quit and never
        // gets the chance to kill it.
        .env("LWT_PARENT_PID", std::process::id().to_string())
        // Python writes .pyc files next to the source it imports, and the source
        // here lives inside the app bundle — so simply running the app dropped a
        // __pycache__ into its own Resources and broke the code signature. codesign
        // then reports a sealed resource missing or invalid, Gatekeeper refuses to
        // open it on anybody else's machine, and macOS can stop honouring the
        // permissions that were granted to it. The bytecode saves a fraction of a
        // second on a process that then runs whisper for minutes.
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .spawn()
        .map_err(|e| match e.kind() {
            ErrorKind::NotFound => "uv disappeared between finding it and running it.".to_string(),
            _ => format!("could not start the backend: {e}"),
        })?;

    let deadline = Instant::now() + START_TIMEOUT;
    while Instant::now() < deadline {
        if port_open() {
            return Ok(Some(child));
        }
        std::thread::sleep(Duration::from_millis(200));
    }
    Err("the backend did not start listening in time.".into())
}

// --- the menu bar ------------------------------------------------------------
//
// The backend already knows everything worth showing, so the menu bar asks it in
// the plainest way there is: one request, one line of text back. No HTTP client,
// no JSON parser, no shared state to keep in step with the window — whatever the
// window would say, the menu bar says, because both are reading the same server.

/// How long to wait on a reply. Most calls answer at once; the one that opens a
/// file picker answers when somebody has finished browsing, which is a different
/// order of time entirely — the backend gives that dialog five minutes, so this
/// has to outlast it. At twenty seconds the reply never arrived, the window was
/// never shown, and a transcription that had started correctly looked like a menu
/// item that did nothing at all.
const QUICK: Duration = Duration::from_secs(20);
const PATIENT: Duration = Duration::from_secs(330);

/// One request to the local server. Loopback and one line long, so it is written
/// out by hand rather than dragging in an HTTP client for it.
fn ask(method: &str, path: &str, patience: Duration) -> Option<String> {
    let mut socket = TcpStream::connect(("127.0.0.1", PORT)).ok()?;
    socket.set_read_timeout(Some(patience)).ok()?;
    write!(
        socket,
        "{method} {path} HTTP/1.1\r\nHost: localhost\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
    )
    .ok()?;
    let mut reply = String::new();
    socket.read_to_string(&mut reply).ok()?;
    // Past the headers; the body is all we ever want and it is never long.
    reply.split_once("\r\n\r\n").map(|(_, body)| body.trim().to_string())
}

/// What the menu bar shows, and what its items offer to do next.
///
/// An empty title is deliberate for the resting state: an icon sitting there
/// quietly is the whole point, and a word beside it every minute of the day is
/// clutter.
struct Glance {
    title: String,
    record: String,
    pause: String,
    can_pause: bool,
}

fn read_glance(line: &str) -> Glance {
    let mut parts = line.split_whitespace();
    let state = parts.next().unwrap_or("idle");
    let number: u64 = parts.next().and_then(|n| n.parse().ok()).unwrap_or(0);
    let clock = format!("{}:{:02}", number / 60, number % 60);
    match state {
        "recording" => Glance {
            title: clock,
            record: "Stop recording".into(),
            pause: "Pause".into(),
            can_pause: true,
        },
        // The clock stops with the recording, so the number here is what is in the
        // file rather than how long ago it began.
        "paused" => Glance {
            title: format!("{clock} paused"),
            record: "Stop recording".into(),
            pause: "Resume".into(),
            can_pause: true,
        },
        // Stopping and saving are brief and not worth a clock, but they must not
        // read as "not recording" either, or the button would offer to start a
        // second one on top of the first.
        "stopping" | "saving" => Glance {
            title: "saving".into(),
            record: "Saving…".into(),
            pause: "Pause".into(),
            can_pause: false,
        },
        "working" => Glance {
            title: format!("{number}%"),
            record: "Start recording".into(),
            pause: "Pause".into(),
            can_pause: false,
        },
        _ => Glance {
            title: String::new(),
            record: "Start recording".into(),
            pause: "Pause".into(),
            can_pause: false,
        },
    }
}

fn show_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

fn main() {
    tauri::Builder::default()
        .manage(Backend(Mutex::new(None)))
        .setup(|app| {
            // A window saying what went wrong beats a bouncing icon that never
            // opens anything, so a failure to start still gets a window.
            let (child, url) = match start_backend(app) {
                Ok(child) => (child, format!("http://127.0.0.1:{PORT}")),
                Err(why) => {
                    let html = format!(
                        "<body style='font:16px/1.6 -apple-system;padding:3rem;color:#17140f'>\
                         <h1 style='font:400 1.4rem Superclarendon,Georgia,serif'>\
                         Local Whisper Transcriber could not start</h1><p>{why}</p></body>"
                    );
                    (None, format!("data:text/html,{}", urlencode(&html)))
                }
            };
            match WebviewWindowBuilder::new(app, "main", WebviewUrl::External(url.parse()?))
                .title("Local Whisper Transcriber")
                .inner_size(1000.0, 820.0)
                .min_inner_size(420.0, 480.0)
                .build()
            {
                Ok(window) => {
                    // The X closes the window, it does not end the app. A meeting
                    // recorder whose window has to stay open is not a background
                    // app at all — and quitting took the menu bar item with it, so
                    // the one thing left to control the recording went too.
                    window.on_window_event({
                        let window = window.clone();
                        move |event| {
                            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                                api.prevent_close();
                                let _ = window.hide();
                            }
                        }
                    });
                    *app.state::<Backend>().0.lock().unwrap() = child;
                    if let Err(e) = build_tray(app.handle()) {
                        // A missing menu bar is not worth refusing to open over.
                        eprintln!("the menu bar item could not be created: {e}");
                    }
                    Ok(())
                }
                // Without this the backend outlives the app that started it.
                Err(e) => {
                    if let Some(mut child) = child {
                        let _ = child.kill();
                        let _ = child.wait();
                    }
                    Err(e.into())
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("failed to start")
        .run(|app, event| match event {
            // No guard against exiting here, on purpose. The window is hidden
            // rather than closed, so it still exists and macOS has no
            // last-window-closed moment to act on — and a guard would have to turn
            // away Cmd-Q and the AppleScript quit rebuild.sh uses along with it,
            // since all three arrive as the same request with the same empty code.
            //
            // Clicking the dock icon of an app with nothing on screen. Without
            // this it would appear to do nothing at all.
            RunEvent::Reopen { .. } => show_window(app),
            RunEvent::Exit => {
                // Quitting stops the transcriber. Nothing of ours outlives the
                // app, however it was ended.
                if let Some(mut child) = app.state::<Backend>().0.lock().unwrap().take() {
                    let _ = child.kill();
                    let _ = child.wait();
                }
            }
            _ => {}
        });
}

/// The menu bar item: a clock while recording, and the two things worth doing
/// without going and finding the window.
fn build_tray(app: &AppHandle) -> tauri::Result<()> {
    let toggle = MenuItem::with_id(app, "toggle", "Start recording", true, None::<&str>)?;
    let hold = MenuItem::with_id(app, "pause", "Pause", false, None::<&str>)?;
    let pick = MenuItem::with_id(app, "pick", "Transcribe a file…", true, None::<&str>)?;
    let open = MenuItem::with_id(app, "open", "Open Local Whisper Transcriber", true, None::<&str>)?;
    let menu = Menu::with_items(
        app,
        &[
            &toggle,
            &hold,
            &PredefinedMenuItem::separator(app)?,
            &pick,
            &PredefinedMenuItem::separator(app)?,
            &open,
            &PredefinedMenuItem::separator(app)?,
            // Ours rather than the predefined one, which asks the system to
            // terminate and would be turned away by the exit guard above along
            // with everything else.
            &MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?,
        ],
    )?;

    let tray = TrayIconBuilder::with_id("menubar")
        // Its own glyph, not the app icon. A template image keeps only the alpha
        // channel and lets macOS paint the shape to suit a light or dark menu bar
        // — so handing it the app icon put a solid black square up there, which is
        // precisely what an opaque icon's alpha channel says it is. See menubar.py.
        .icon(tauri::image::Image::from_bytes(include_bytes!("../icons/menubar.png"))?)
        .icon_as_template(true)
        .menu(&menu)
        // The menu, on either button. This was set to open the window on a left
        // click instead, which is not what a menu bar item does anywhere else on
        // the system: clicking it did nothing visible, and the menu could only be
        // reached with two fingers — control-click did not reach it either.
        // Opening the window is an item in the menu, where it can be read.
        .show_menu_on_left_click(true)
        .on_menu_event(|app: &AppHandle, event: MenuEvent| match event.id().as_ref() {
            // On its own thread: this waits for the backend, and the backend waits
            // for audio devices to start. Doing it here would freeze the menu bar
            // for as long as that takes, which on a cold microphone is seconds.
            "toggle" => {
                std::thread::spawn(|| {
                    ask("POST", "/api/record/toggle", QUICK);
                });
            }
            "pause" => {
                std::thread::spawn(|| {
                    ask("POST", "/api/record/pause", QUICK);
                });
            }
            // The picker sits there until somebody answers it, and the window is
            // where anything that goes wrong gets said.
            "pick" => {
                let app = app.clone();
                std::thread::spawn(move || {
                    match ask("POST", "/api/transcribe/pick", PATIENT) {
                        // Started, or refused for a reason worth reading — either
                        // way the window is where it can be seen. Only a cancelled
                        // picker leaves the screen alone, because somebody who
                        // changed their mind has not asked for anything.
                        Some(body) if body.contains("\"started\":true")
                            || body.contains("\"detail\"") => show_window(&app),
                        _ => {}
                    }
                });
            }
            "open" => show_window(app),
            "quit" => app.exit(0),
            _ => {}
        })
        .build(app)?;
    // Nothing beside the icon until there is something to say. The loop below only
    // wakes a second from now, and a title appearing late reads as a glitch.
    tray.set_title(Some(""))?;

    // The clock. A second is the right rate for something showing minutes and
    // seconds, and it is the same rate the window polls at.
    let handle = app.clone();
    std::thread::spawn(move || loop {
        std::thread::sleep(Duration::from_secs(1));
        let line = ask("GET", "/api/glance", QUICK).unwrap_or_default();
        let now = read_glance(&line);
        if let Some(tray) = handle.tray_by_id("menubar") {
            let _ = tray.set_title(Some(now.title));
        }
        let _ = toggle.set_text(now.record);
        let _ = hold.set_text(now.pause);
        // Greyed rather than hidden when there is nothing to pause: a menu whose
        // items move about is harder to use than one whose items stay put.
        let _ = hold.set_enabled(now.can_pause);
    });
    Ok(())
}

fn urlencode(s: &str) -> String {
    s.bytes()
        .map(|b| match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                (b as char).to_string()
            }
            b' ' => "%20".to_string(),
            other => format!("%{other:02X}"),
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::read_glance;

    // The only branching logic on this side. Everything else the menu bar does is
    // asking the backend, which the Python suite covers.
    #[test]
    fn a_recording_reads_as_a_clock() {
        assert_eq!(read_glance("recording 42").title, "0:42");
        assert_eq!(read_glance("recording 605").title, "10:05");
        assert_eq!(read_glance("recording 42").record, "Stop recording");
    }

    #[test]
    fn nothing_happening_shows_nothing_beside_the_icon() {
        let idle = read_glance("idle");
        assert_eq!(idle.title, "");
        assert_eq!(idle.record, "Start recording");
    }

    #[test]
    fn saving_never_offers_to_start_a_second_recording() {
        for line in ["stopping 12", "saving 12"] {
            assert_ne!(read_glance(line).record, "Start recording", "{line}");
        }
    }

    #[test]
    fn a_transcription_shows_its_progress() {
        assert_eq!(read_glance("working 63").title, "63%");
    }

    // Pause is offered only while there is something to pause, and says which way
    // it would go — a menu item reading "Pause" on an already paused recording is
    // a button that lies about what it does.
    #[test]
    fn pause_is_offered_only_when_it_means_something() {
        let running = read_glance("recording 42");
        assert!(running.can_pause && running.pause == "Pause");
        let held = read_glance("paused 42");
        assert!(held.can_pause && held.pause == "Resume");
        assert!(held.title.contains("paused"), "{}", held.title);
        for line in ["idle", "working 10", "saving 3"] {
            assert!(!read_glance(line).can_pause, "{line}");
        }
    }

    // A paused recording can still be stopped. Offering only "Resume" would leave
    // somebody who paused and changed their mind with no way to end it.
    #[test]
    fn a_paused_recording_can_still_be_stopped() {
        assert_eq!(read_glance("paused 42").record, "Stop recording");
    }

    // A backend that is not answering yet hands back an empty string, and a menu
    // bar that panicked on it would take the whole app down on launch.
    #[test]
    fn an_unanswered_backend_is_not_a_crash() {
        assert_eq!(read_glance("").title, "");
        assert_eq!(read_glance("recording").title, "0:00");
        assert_eq!(read_glance("recording not-a-number").title, "0:00");
    }
}
