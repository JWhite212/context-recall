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
fn resolve_daemon_binary(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    app.path()
        .resource_dir()
        .map(|p| p.join("context-recall-daemon"))
        .map_err(|e| e.to_string())
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
    app.path()
        .resource_dir()
        .map(|p| p.join("context-recall-daemon").display().to_string())
        .map_err(|e| e.to_string())
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
        ])
        .setup(|app| {
            tray::setup(app)?;
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
