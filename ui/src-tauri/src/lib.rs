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

/// Render the LaunchAgent plist XML for the given daemon binary path.
fn render_launch_agent_plist(daemon_path: &str, home: &Path) -> String {
    let logs_dir = home
        .join("Library")
        .join("Logs")
        .join("Context Recall");
    let stdout_path = logs_dir.join("launchagent.out.log");
    let stderr_path = logs_dir.join("launchagent.err.log");
    format!(
        r#"<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
      <string>{daemon}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
      <key>SuccessfulExit</key>
      <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>StandardOutPath</key>
    <string>{stdout}</string>
    <key>StandardErrorPath</key>
    <string>{stderr}</string>
  </dict>
</plist>
"#,
        label = LAUNCH_AGENT_LABEL,
        daemon = daemon_path,
        stdout = stdout_path.display(),
        stderr = stderr_path.display(),
    )
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
    let contents = render_launch_agent_plist(&daemon.display().to_string(), &home);

    fs::write(&path, contents)
        .map_err(|e| format!("Failed to write plist {}: {}", path.display(), e))?;
    Ok(())
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
