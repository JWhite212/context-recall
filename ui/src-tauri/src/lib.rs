use std::fs;
use std::path::PathBuf;

mod tray;

/// Read the shared auth token so the frontend can authenticate with the API.
#[tauri::command]
fn read_auth_token() -> Result<String, String> {
    let path: PathBuf = dirs::config_dir()
        .unwrap_or_else(|| PathBuf::from("~/.config"))
        .join("meetingmind")
        .join("auth_token");

    fs::read_to_string(&path)
        .map(|s| s.trim().to_string())
        .map_err(|e| format!("Failed to read auth token at {}: {}", path.display(), e))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![
            read_auth_token,
            tray::update_tray_state,
        ])
        .setup(|app| {
            tray::setup(app)?;
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
