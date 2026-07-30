// A window around the local backend, and nothing more.
//
// The app owns the backend's lifetime: it starts it on launch and kills it on
// quit, so "is this running?" has the same answer as "is the window open?".
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::ErrorKind;
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};

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
