use std::fs;
use std::path::PathBuf;
use std::sync::Mutex;

use tauri::Manager;

mod tray;

/// Stores the pending update so install_update doesn't need to re-check.
struct PendingUpdate(Mutex<Option<tauri_plugin_updater::Update>>);

/// Read the shared auth token so the frontend can authenticate with the API.
#[tauri::command]
fn read_auth_token() -> Result<String, String> {
    let home = dirs::home_dir().unwrap_or_else(|| PathBuf::from("/tmp"));
    let path: PathBuf = home.join(".config").join("meetingmind").join("auth_token");

    fs::read_to_string(&path)
        .map(|s| s.trim().to_string())
        .map_err(|e| format!("Failed to read auth token at {}: {}", path.display(), e))
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
        .map(|p| p.join("meetingmind-daemon").display().to_string())
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
