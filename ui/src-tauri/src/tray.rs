use tauri::{
    image::Image,
    menu::{Menu, MenuItem, PredefinedMenuItem},
    tray::TrayIconBuilder,
    AppHandle, Emitter, Manager, Wry,
};

const ICON_SIZE: u32 = 44;
const DOT_RADIUS: f32 = 8.0;

/// Render a small anti-aliased colored dot as raw RGBA pixels (44x44 for retina).
fn dot_icon_rgba(r: u8, g: u8, b: u8) -> Vec<u8> {
    let s = ICON_SIZE;
    let c = s as f32 / 2.0;
    let mut buf = vec![0u8; (s * s * 4) as usize];

    for y in 0..s {
        for x in 0..s {
            let dx = x as f32 - c;
            let dy = y as f32 - c;
            let dist = (dx * dx + dy * dy).sqrt();
            if dist <= DOT_RADIUS + 0.5 {
                let a = ((DOT_RADIUS + 0.5 - dist).clamp(0.0, 1.0) * 255.0) as u8;
                let i = ((y * s + x) * 4) as usize;
                buf[i] = r;
                buf[i + 1] = g;
                buf[i + 2] = b;
                buf[i + 3] = a;
            }
        }
    }

    buf
}

fn build_menu(
    handle: &AppHandle,
    is_recording: bool,
    status_line: &str,
) -> tauri::Result<Menu<Wry>> {
    let status = MenuItem::with_id(handle, "status", status_line, false, None::<&str>)?;
    let sep1 = PredefinedMenuItem::separator(handle)?;
    let rec = if is_recording {
        MenuItem::with_id(handle, "stop_recording", "Stop Recording", true, None::<&str>)?
    } else {
        MenuItem::with_id(handle, "start_recording", "Start Recording", true, None::<&str>)?
    };
    let sep2 = PredefinedMenuItem::separator(handle)?;
    let open = MenuItem::with_id(handle, "open", "Open MeetingMind", true, None::<&str>)?;
    let prefs = MenuItem::with_id(
        handle,
        "preferences",
        "Preferences\u{2026}",
        true,
        None::<&str>,
    )?;
    let sep3 = PredefinedMenuItem::separator(handle)?;
    let quit = MenuItem::with_id(handle, "quit", "Quit MeetingMind", true, None::<&str>)?;

    Menu::with_items(
        handle,
        &[&status, &sep1, &rec, &sep2, &open, &prefs, &sep3, &quit],
    )
}

pub fn setup(app: &tauri::App) -> tauri::Result<()> {
    let menu = build_menu(app.handle(), false, "Idle \u{2014} No active meeting")?;

    TrayIconBuilder::with_id("main")
        .icon(app.default_window_icon().unwrap().clone())
        .tooltip("MeetingMind")
        .menu(&menu)
        .on_menu_event(|app_handle, event| match event.id.as_ref() {
            "open" => {
                if let Some(w) = app_handle.get_webview_window("main") {
                    let _ = w.show();
                    let _ = w.set_focus();
                }
            }
            "preferences" => {
                if let Some(w) = app_handle.get_webview_window("main") {
                    let _ = w.show();
                    let _ = w.set_focus();
                }
                let _ = app_handle.emit("tray-action", "preferences");
            }
            "start_recording" | "stop_recording" => {
                let _ = app_handle.emit("tray-action", event.id.as_ref().to_string());
            }
            "quit" => app_handle.exit(0),
            _ => {}
        })
        .build(app)?;

    Ok(())
}

#[tauri::command]
pub fn update_tray_state(
    app: AppHandle,
    state: String,
    meeting_title: Option<String>,
) -> Result<(), String> {
    let tray = app.tray_by_id("main").ok_or("Tray icon not found")?;

    let is_recording = state == "recording";

    // Tooltip.
    let tooltip = match state.as_str() {
        "idle" => "MeetingMind \u{2014} Idle".to_string(),
        "detecting" => "MeetingMind \u{2014} Detecting...".to_string(),
        "recording" => match &meeting_title {
            Some(t) => format!("MeetingMind \u{2014} Recording: {t}"),
            None => "MeetingMind \u{2014} Recording".to_string(),
        },
        "processing" => "MeetingMind \u{2014} Processing...".to_string(),
        _ => "MeetingMind".to_string(),
    };

    // Menu status line.
    let status_line = match state.as_str() {
        "idle" => "Idle \u{2014} No active meeting",
        "detecting" => "Detecting meeting...",
        "recording" => "Recording in progress",
        "processing" => "Processing meeting...",
        _ => "MeetingMind",
    };

    // Icon colour per state.
    let (r, g, b) = match state.as_str() {
        "idle" => (120, 190, 120),      // green
        "detecting" => (220, 180, 60),   // amber
        "recording" => (220, 80, 80),    // red
        "processing" => (100, 140, 220), // blue
        _ => (120, 120, 120),            // grey
    };

    let rgba = dot_icon_rgba(r, g, b);
    let icon = Image::new(&rgba, ICON_SIZE, ICON_SIZE);
    tray.set_icon(Some(icon)).map_err(|e| e.to_string())?;
    tray.set_tooltip(Some(&tooltip)).map_err(|e| e.to_string())?;

    let menu = build_menu(&app, is_recording, status_line).map_err(|e| e.to_string())?;
    tray.set_menu(Some(menu)).map_err(|e| e.to_string())?;

    Ok(())
}
