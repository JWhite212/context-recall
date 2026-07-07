use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use tauri::Manager;

mod tray;

/// Stores the pending update so install_update doesn't need to re-check.
struct PendingUpdate(Mutex<Option<tauri_plugin_updater::Update>>);

const LAUNCH_AGENT_LABEL: &str = "dev.jamiewhite.contextrecall.agent";

/// Resolve the absolute path to the LaunchAgent plist.
fn launch_agent_plist_path() -> Result<PathBuf, String> {
    let home = dirs::home_dir().ok_or_else(|| "Cannot resolve home directory".to_string())?;
    Ok(home
        .join("Library")
        .join("LaunchAgents")
        .join(format!("{LAUNCH_AGENT_LABEL}.plist")))
}

/// Resolve the bundled daemon binary path (mirrors `daemon_binary_path`).
///
/// The bundle declares `resources/context-recall-daemon`, which Tauri
/// places at `Contents/Resources/resources/context-recall-daemon/`.
/// Since 2026-07 that directory holds a minimal `Context Recall
/// Daemon.app` wrapper: TCC kills any process that requests microphone
/// access without an Info.plist carrying NSMicrophoneUsageDescription
/// (observed as an OS_REASON_TCC launchd crash loop), and only a bundle
/// can carry one — so the LaunchAgent must exec the executable inside
/// the wrapper.
fn resolve_daemon_binary(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    app.path()
        .resource_dir()
        .map(|p| {
            p.join("resources")
                .join("context-recall-daemon")
                .join("Context Recall Daemon.app")
                .join("Contents")
                .join("MacOS")
                .join("context-recall-daemon")
        })
        .map_err(|e| e.to_string())
}

/// Extract `ProgramArguments[0]` from a LaunchAgent plist on disk.
fn plist_program_path(path: &Path) -> Option<PathBuf> {
    let value = plist::Value::from_file(path).ok()?;
    let dict = value.as_dictionary()?;
    let args = dict.get("ProgramArguments")?.as_array()?;
    let first = args.first()?.as_string()?;
    Some(PathBuf::from(first))
}

fn current_uid() -> Result<String, String> {
    let out = std::process::Command::new("id")
        .arg("-u")
        .output()
        .map_err(|e| format!("Failed to run id -u: {e}"))?;
    if !out.status.success() {
        return Err("id -u failed".into());
    }
    Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
}

fn launchctl(args: &[&str]) -> Result<std::process::Output, String> {
    std::process::Command::new("launchctl")
        .args(args)
        .output()
        .map_err(|e| format!("Failed to run launchctl: {e}"))
}

/// Ensure the daemon LaunchAgent is installed and the daemon is running.
///
/// Silent, idempotent bootstrap: installs (or repairs) the plist when its
/// target binary is missing, loads the service with `launchctl bootstrap`
/// (tolerated when already loaded), and starts it with a gentle
/// `launchctl kickstart` (no `-k`, so a running daemon is untouched).
/// An existing plist whose target binary exists is left exactly as the
/// user configured it (e.g. a dev venv invocation).
fn ensure_daemon_running(app: &tauri::AppHandle) -> Result<String, String> {
    let plist_path = launch_agent_plist_path()?;
    let bundled = resolve_daemon_binary(app)?;
    let existing_target_ok = plist_path.exists()
        && plist_program_path(&plist_path)
            .map(|p| p.exists())
            .unwrap_or(false);

    let uid = current_uid()?;
    let service = format!("gui/{uid}/{LAUNCH_AGENT_LABEL}");
    let mut actions: Vec<&str> = Vec::new();

    if !existing_target_ok {
        if !bundled.exists() {
            if plist_path.exists() {
                return Err(format!(
                    "LaunchAgent at {} points at a missing daemon binary and no \
                     bundled daemon is available to repair it.",
                    plist_path.display()
                ));
            }
            return Err(
                "No daemon available: no LaunchAgent installed and the bundled \
                 daemon binary is missing (development build?)."
                    .into(),
            );
        }
        if let Some(parent) = plist_path.parent() {
            fs::create_dir_all(parent)
                .map_err(|e| format!("Failed to create {}: {}", parent.display(), e))?;
        }
        // A stale service with this label may still be loaded from the old
        // plist — boot it out first (tolerated when not loaded).
        let _ = launchctl(&["bootout", &service]);
        let home = dirs::home_dir().ok_or_else(|| "Cannot resolve home directory".to_string())?;
        write_launch_agent_plist(&plist_path, &bundled.display().to_string(), &home)?;
        actions.push("installed LaunchAgent");
    }

    // Load if not already loaded; bootstrap fails harmlessly when loaded.
    let plist_str = plist_path.display().to_string();
    let bootstrap = launchctl(&["bootstrap", &format!("gui/{uid}"), &plist_str])?;
    if bootstrap.status.success() {
        actions.push("loaded service");
    }

    let kick = launchctl(&["kickstart", &service])?;
    if !kick.status.success() {
        return Err(format!(
            "launchctl kickstart failed: {}",
            String::from_utf8_lossy(&kick.stderr).trim()
        ));
    }
    actions.push("daemon running");
    Ok(actions.join("; "))
}

/// Start (or repair + start) the local daemon. Invoked automatically by
/// the frontend when the daemon is unreachable, and available from the
/// connection screen's "Start local service" button.
#[tauri::command]
fn start_daemon(app: tauri::AppHandle) -> Result<String, String> {
    ensure_daemon_running(&app)
}

/// Build the LaunchAgent plist as a structured dictionary.
///
/// Using `plist::Dictionary` instead of `format!()`-style XML guarantees
/// that any character in the daemon path, home dir, or future fields is
/// XML-escaped properly — so a username containing `<`, `>`, `&`, `"`,
/// or `'` can't corrupt the plist and break auto-start.
fn build_launch_agent_plist(daemon_path: &str, home: &Path) -> plist::Value {
    let logs_dir = home
        .join("Library")
        .join("Logs")
        .join("Context Recall");
    let stdout_path = logs_dir.join("launchagent.out.log");
    let stderr_path = logs_dir.join("launchagent.err.log");

    let mut keep_alive = plist::Dictionary::new();
    keep_alive.insert(
        "SuccessfulExit".into(),
        plist::Value::Boolean(false),
    );

    // Inherit a stable PATH under launchd so pgrep/lsof/osascript resolve.
    // launchd does NOT inherit the user's shell PATH, so without this the
    // daemon's platform-detection helpers can silently fail.
    let mut environment = plist::Dictionary::new();
    environment.insert(
        "PATH".into(),
        plist::Value::String("/usr/bin:/bin:/usr/sbin:/sbin".into()),
    );

    let mut dict = plist::Dictionary::new();
    dict.insert("Label".into(), plist::Value::String(LAUNCH_AGENT_LABEL.into()));
    dict.insert(
        "ProgramArguments".into(),
        plist::Value::Array(vec![plist::Value::String(daemon_path.into())]),
    );
    dict.insert("RunAtLoad".into(), plist::Value::Boolean(true));
    dict.insert("KeepAlive".into(), plist::Value::Dictionary(keep_alive));
    dict.insert("ThrottleInterval".into(), plist::Value::Integer(30.into()));
    dict.insert(
        "StandardOutPath".into(),
        plist::Value::String(stdout_path.display().to_string()),
    );
    dict.insert(
        "StandardErrorPath".into(),
        plist::Value::String(stderr_path.display().to_string()),
    );
    dict.insert(
        "EnvironmentVariables".into(),
        plist::Value::Dictionary(environment),
    );

    plist::Value::Dictionary(dict)
}

/// Atomically write the LaunchAgent plist to `path`.
///
/// Writes to a sibling `.tmp` file then renames into place, so an app
/// crash mid-write can't leave a corrupt half-written plist that breaks
/// auto-start on next login.
fn write_launch_agent_plist(path: &Path, daemon_path: &str, home: &Path) -> Result<(), String> {
    let value = build_launch_agent_plist(daemon_path, home);

    let tmp_path = match path.file_name() {
        Some(name) => {
            let mut tmp_name = name.to_os_string();
            tmp_name.push(".tmp");
            path.with_file_name(tmp_name)
        }
        None => return Err(format!("Invalid plist path: {}", path.display())),
    };

    {
        let tmp_file = fs::File::create(&tmp_path).map_err(|e| {
            format!("Failed to create {}: {}", tmp_path.display(), e)
        })?;
        plist::to_writer_xml(tmp_file, &value).map_err(|e| {
            // Clean up the partial temp file on serialisation failure.
            let _ = fs::remove_file(&tmp_path);
            format!("Failed to serialize plist: {e}")
        })?;
    }

    fs::rename(&tmp_path, path).map_err(|e| {
        let _ = fs::remove_file(&tmp_path);
        format!(
            "Failed to atomically install plist at {}: {}",
            path.display(),
            e
        )
    })
}

/// Return whether the LaunchAgent plist currently exists.
#[tauri::command]
fn is_start_at_login_enabled() -> Result<bool, String> {
    let path = launch_agent_plist_path()?;
    Ok(path.exists())
}

/// Write or remove the LaunchAgent plist. Idempotent: existing plist is
/// always removed first before writing. Does not invoke `launchctl load`;
/// the user must sign out/in (or run `launchctl load`) to activate.
#[tauri::command]
fn set_start_at_login(app: tauri::AppHandle, enabled: bool) -> Result<(), String> {
    let path = launch_agent_plist_path()?;

    // Idempotent removal: attempt deletion and tolerate NotFound.
    match fs::remove_file(&path) {
        Ok(()) => {}
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => {
            return Err(format!(
                "Failed to remove existing plist {}: {}",
                path.display(),
                e
            ));
        }
    }

    if !enabled {
        return Ok(());
    }

    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|e| format!("Failed to create {}: {}", parent.display(), e))?;
    }

    let daemon = resolve_daemon_binary(&app)?;
    let home = dirs::home_dir().ok_or_else(|| "Cannot resolve home directory".to_string())?;

    write_launch_agent_plist(&path, &daemon.display().to_string(), &home)
}

/// Read the shared auth token so the frontend can authenticate with the API.
///
/// Refuses to return the token unless the file's POSIX mode is exactly
/// `0o600` (owner read/write only). If another user-readable bit is set,
/// the secret is treated as compromised and an error is returned instead
/// of leaking the value to the frontend.
#[tauri::command]
fn read_auth_token() -> Result<String, String> {
    let home = dirs::home_dir().unwrap_or_else(|| PathBuf::from("/tmp"));
    let path: PathBuf = home
        .join("Library")
        .join("Application Support")
        .join("Context Recall")
        .join("auth_token");

    let contents = fs::read_to_string(&path)
        .map_err(|e| format!("Failed to read auth token at {}: {}", path.display(), e))?;

    let metadata = fs::metadata(&path)
        .map_err(|e| format!("Failed to stat auth token at {}: {}", path.display(), e))?;
    let mode = metadata.permissions().mode() & 0o777;
    if mode != 0o600 {
        return Err(format!(
            "Auth token at {} has insecure mode {:o}; expected 600",
            path.display(),
            mode
        ));
    }

    Ok(contents.trim().to_string())
}

/// Check for app updates and return version info if available.
#[tauri::command]
async fn check_for_updates(app: tauri::AppHandle) -> Result<Option<String>, String> {
    use tauri_plugin_updater::UpdaterExt;

    match app.updater().map_err(|e| e.to_string())?.check().await {
        Ok(Some(update)) => {
            let version = update.version.clone();
            let state = app.state::<PendingUpdate>();
            *state.0.lock().unwrap() = Some(update);
            Ok(Some(version))
        }
        Ok(None) => Ok(None),
        Err(e) => Err(format!("Update check failed: {e}")),
    }
}

/// Return the absolute path to the bundled daemon binary.
#[tauri::command]
fn daemon_binary_path(app: tauri::AppHandle) -> Result<String, String> {
    resolve_daemon_binary(&app).map(|p| p.display().to_string())
}

/// Reveal the Context Recall logs folder in Finder.
#[tauri::command]
fn open_logs_dir(app: tauri::AppHandle) -> Result<(), String> {
    use tauri_plugin_opener::OpenerExt;

    let home = dirs::home_dir().ok_or_else(|| "Cannot resolve home directory".to_string())?;
    let logs = home.join("Library").join("Logs").join("Context Recall");
    fs::create_dir_all(&logs)
        .map_err(|e| format!("Failed to create {}: {}", logs.display(), e))?;
    app.opener()
        .open_path(logs.display().to_string(), None::<&str>)
        .map_err(|e| e.to_string())
}

/// Reveal the Context Recall application support folder in Finder.
#[tauri::command]
fn open_app_support_dir(app: tauri::AppHandle) -> Result<(), String> {
    use tauri_plugin_opener::OpenerExt;

    let home = dirs::home_dir().ok_or_else(|| "Cannot resolve home directory".to_string())?;
    let support = home
        .join("Library")
        .join("Application Support")
        .join("Context Recall");
    fs::create_dir_all(&support)
        .map_err(|e| format!("Failed to create {}: {}", support.display(), e))?;
    app.opener()
        .open_path(support.display().to_string(), None::<&str>)
        .map_err(|e| e.to_string())
}

/// Open a specific macOS System Settings pane. Targets are limited to an
/// explicit allowlist so callers cannot pass arbitrary `x-apple.*` URLs
/// (which would let the frontend deep-link to anywhere on the system).
#[tauri::command]
fn open_macos_settings(app: tauri::AppHandle, target: &str) -> Result<(), String> {
    use tauri_plugin_opener::OpenerExt;

    // Allowlist of supported deep-links. Each entry maps a logical name to
    // either an `x-apple.systempreferences:` URL or, for Audio MIDI Setup
    // (a separate utility, not a Settings pane), the bundled app's URL.
    let url = match target {
        "audio-midi-setup" => "file:///System/Applications/Utilities/Audio%20MIDI%20Setup.app",
        "privacy-microphone" => {
            "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
        }
        "privacy-screen-recording" => {
            "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"
        }
        "sound" => "x-apple.systempreferences:com.apple.preference.sound",
        _ => return Err(format!("Unsupported settings target: {target}")),
    };

    app.opener()
        .open_url(url, None::<&str>)
        .map_err(|e| e.to_string())
}

/// Download and install the pending update found by check_for_updates.
#[tauri::command]
async fn install_update(app: tauri::AppHandle) -> Result<(), String> {
    let update = {
        let state = app.state::<PendingUpdate>();
        let taken = state.0.lock().unwrap().take();
        taken
    }
    .ok_or_else(|| "No pending update — check for updates first".to_string())?;

    update
        .download_and_install(|_, _| {}, || {})
        .await
        .map_err(|e| e.to_string())?;

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn plist_program_path_reads_first_program_argument() {
        let dir = std::env::temp_dir().join(format!("cr-plist-test-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("agent.plist");
        write_launch_agent_plist(&path, "/opt/daemon/context-recall-daemon", Path::new("/Users/test")).unwrap();

        let program = plist_program_path(&path);
        assert_eq!(program, Some(PathBuf::from("/opt/daemon/context-recall-daemon")));
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn plist_program_path_none_for_missing_or_invalid() {
        assert_eq!(plist_program_path(Path::new("/nonexistent/agent.plist")), None);

        let dir = std::env::temp_dir().join(format!("cr-plist-bad-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("bad.plist");
        fs::write(&path, "not a plist").unwrap();
        assert_eq!(plist_program_path(&path), None);
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn launch_agent_plist_keeps_daemon_alive_and_runs_at_load() {
        let value = build_launch_agent_plist("/opt/daemon/bin", Path::new("/Users/test"));
        let dict = value.as_dictionary().unwrap();
        assert_eq!(
            dict.get("Label").and_then(|v| v.as_string()),
            Some(LAUNCH_AGENT_LABEL)
        );
        assert_eq!(dict.get("RunAtLoad").and_then(|v| v.as_boolean()), Some(true));
        assert!(dict.get("KeepAlive").is_some());
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(PendingUpdate(Mutex::new(None)))
        .invoke_handler(tauri::generate_handler![
            read_auth_token,
            tray::update_tray_state,
            check_for_updates,
            install_update,
            daemon_binary_path,
            open_logs_dir,
            open_app_support_dir,
            open_macos_settings,
            is_start_at_login_enabled,
            set_start_at_login,
            start_daemon,
        ])
        .setup(|app| {
            tray::setup(app)?;
            // Silent daemon bootstrap: bring the daemon up on every app
            // launch without user input. Off the main thread — launchctl
            // calls must not block window creation. Failures are logged
            // only; the frontend retries via start_daemon with UI feedback.
            let handle = app.handle().clone();
            std::thread::spawn(move || match ensure_daemon_running(&handle) {
                Ok(status) => eprintln!("[daemon-bootstrap] {status}"),
                Err(e) => eprintln!("[daemon-bootstrap] skipped: {e}"),
            });
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
