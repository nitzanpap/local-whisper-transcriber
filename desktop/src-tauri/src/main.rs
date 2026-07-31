// A window around the local backend, and nothing more.
//
// The app owns the backend's lifetime: it starts it on launch and kills it on
// quit, so "is this running?" has the same answer as "is the window open?".
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

/// One request to the local server. Loopback and one line long, so it is written
/// out by hand rather than dragging in an HTTP client for it.
fn ask(method: &str, path: &str) -> Option<String> {
    let mut socket = TcpStream::connect(("127.0.0.1", PORT)).ok()?;
    socket
        .set_read_timeout(Some(Duration::from_secs(20)))
        .ok()?;
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

/// What to put in the menu bar, from the line the backend hands back.
///
/// Returned as (what the menu bar shows, what the first menu item offers). An
/// empty title is deliberate for the resting state: an icon sitting there quietly
/// is the whole point, and a word next to it every minute of the day is clutter.
fn read_glance(line: &str) -> (String, String) {
    let mut parts = line.split_whitespace();
    let state = parts.next().unwrap_or("idle");
    let number: u64 = parts.next().and_then(|n| n.parse().ok()).unwrap_or(0);
    match state {
        "recording" => (
            format!("{}:{:02}", number / 60, number % 60),
            "Stop recording".into(),
        ),
        // Stopping and saving are brief and not worth a clock, but they must not
        // read as "not recording" either, or the button would offer to start a
        // second one on top of the first.
        "stopping" | "saving" => ("saving".into(), "Saving…".into()),
        "working" => (format!("{number}%"), "Start recording".into()),
        _ => (String::new(), "Start recording".into()),
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
                Ok(_) => {
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
        .run(|app, event| {
            if let RunEvent::Exit = event {
                // Quitting the app stops the transcriber. Nothing of ours keeps
                // running once the window is closed.
                if let Some(mut child) = app.state::<Backend>().0.lock().unwrap().take() {
                    let _ = child.kill();
                    let _ = child.wait();
                }
            }
        });
}

/// The menu bar item: a clock while recording, and the two things worth doing
/// without going and finding the window.
fn build_tray(app: &AppHandle) -> tauri::Result<()> {
    let toggle = MenuItem::with_id(app, "toggle", "Start recording", true, None::<&str>)?;
    let open = MenuItem::with_id(app, "open", "Open Local Whisper Transcriber", true, None::<&str>)?;
    let menu = Menu::with_items(
        app,
        &[
            &toggle,
            &PredefinedMenuItem::separator(app)?,
            &open,
            &PredefinedMenuItem::separator(app)?,
            &PredefinedMenuItem::quit(app, Some("Quit"))?,
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
        // The icon opens the window; the menu is the right-hand button and the
        // items below. Without this, clicking it only ever shows the menu.
        .show_menu_on_left_click(false)
        .on_menu_event(|app: &AppHandle, event: MenuEvent| match event.id().as_ref() {
            // On its own thread: this waits for the backend, and the backend waits
            // for audio devices to start. Doing it here would freeze the menu bar
            // for as long as that takes, which on a cold microphone is seconds.
            "toggle" => {
                std::thread::spawn(|| {
                    ask("POST", "/api/record/toggle");
                });
            }
            "open" => show_window(app),
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
        let line = ask("GET", "/api/glance").unwrap_or_default();
        let (title, verb) = read_glance(&line);
        if let Some(tray) = handle.tray_by_id("menubar") {
            let _ = tray.set_title(Some(title));
        }
        let _ = toggle.set_text(verb);
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
        assert_eq!(read_glance("recording 42").0, "0:42");
        assert_eq!(read_glance("recording 605").0, "10:05");
        assert_eq!(read_glance("recording 42").1, "Stop recording");
    }

    #[test]
    fn nothing_happening_shows_nothing_beside_the_icon() {
        assert_eq!(read_glance("idle"), (String::new(), "Start recording".into()));
    }

    #[test]
    fn saving_never_offers_to_start_a_second_recording() {
        for line in ["stopping 12", "saving 12"] {
            assert_ne!(read_glance(line).1, "Start recording", "{line}");
        }
    }

    #[test]
    fn a_transcription_shows_its_progress() {
        assert_eq!(read_glance("working 63").0, "63%");
    }

    // A backend that is not answering yet hands back an empty string, and a menu
    // bar that panicked on it would take the whole app down on launch.
    #[test]
    fn an_unanswered_backend_is_not_a_crash() {
        assert_eq!(read_glance("").0, "");
        assert_eq!(read_glance("recording").0, "0:00");
        assert_eq!(read_glance("recording not-a-number").0, "0:00");
    }
}
