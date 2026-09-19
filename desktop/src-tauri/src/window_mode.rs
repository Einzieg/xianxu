use std::sync::Mutex;
use tauri::{LogicalSize, Manager, PhysicalPosition, PhysicalSize, WebviewWindow};

struct SavedWindow {
    position: PhysicalPosition<i32>,
    size: PhysicalSize<u32>,
    maximized: bool,
    on_top: bool,
    resizable: bool,
}

#[derive(Default)]
pub struct CompactState(Mutex<Option<SavedWindow>>);

fn restore(window: &WebviewWindow, saved: &SavedWindow) -> tauri::Result<()> {
    window.set_resizable(saved.resizable)?;
    window.set_min_size(Some(LogicalSize::new(980.0, 680.0)))?;
    window.set_size(saved.size)?;
    window.set_position(saved.position)?;
    window.set_always_on_top(saved.on_top)?;
    if saved.maximized {
        window.maximize()?;
    }
    Ok(())
}

#[tauri::command]
pub fn set_compact(
    window: WebviewWindow,
    state: tauri::State<'_, CompactState>,
    compact: bool,
) -> Result<bool, String> {
    let mut saved = state.0.lock().map_err(|e| e.to_string())?;
    if compact == saved.is_some() {
        return Ok(compact);
    }
    if !compact {
        restore(&window, saved.as_ref().unwrap()).map_err(|e| e.to_string())?;
        *saved = None;
        return Ok(false);
    }
    let monitor = window
        .current_monitor()
        .map_err(|e| e.to_string())?
        .ok_or("Cannot locate the player's monitor")?;
    let maximized = window.is_maximized().map_err(|e| e.to_string())?;
    if maximized {
        window.unmaximize().map_err(|e| e.to_string())?;
    }
    let previous = SavedWindow {
        position: window.outer_position().map_err(|e| e.to_string())?,
        size: window.inner_size().map_err(|e| e.to_string())?,
        maximized,
        on_top: window.is_always_on_top().map_err(|e| e.to_string())?,
        resizable: window.is_resizable().map_err(|e| e.to_string())?,
    };
    let apply = || -> tauri::Result<()> {
        window.set_min_size(Some(LogicalSize::new(380.0, 240.0)))?;
        window.set_size(LogicalSize::new(420.0, 250.0))?;
        let margin = (12.0 * monitor.scale_factor()).round() as i32;
        let origin = monitor.work_area().position;
        window.set_position(PhysicalPosition::new(origin.x + margin, origin.y + margin))?;
        window.set_resizable(false)?;
        window.set_always_on_top(true)?;
        Ok(())
    };
    if let Err(error) = apply() {
        let rollback = restore(&window, &previous);
        // Keep the recovery geometry if rollback fails, so Expand can retry it.
        if let Err(recovery) = rollback {
            *saved = Some(previous);
            return Err(format!(
                "Mini player failed: {error}; restore failed: {recovery}"
            ));
        }
        return Err(error.to_string());
    }
    *saved = Some(previous);
    Ok(true)
}

pub fn smoke(window: &WebviewWindow) -> Result<String, String> {
    let check = || -> tauri::Result<_> {
        Ok((
            window.inner_size()?,
            window.outer_position()?,
            window.is_always_on_top()?,
        ))
    };
    let before = check().map_err(|e| e.to_string())?;
    let monitor = window
        .current_monitor()
        .map_err(|e| e.to_string())?
        .ok_or("No monitor")?;
    set_compact(window.clone(), window.state::<CompactState>(), true)?;
    let small = check().map_err(|e| e.to_string())?;
    let scale = window.scale_factor().map_err(|e| e.to_string())?;
    let expected_size: PhysicalSize<u32> = LogicalSize::new(420.0, 250.0).to_physical(scale);
    let margin = (12.0 * monitor.scale_factor()).round() as i32;
    let origin = monitor.work_area().position;
    let expected_position = PhysicalPosition::new(origin.x + margin, origin.y + margin);
    set_compact(window.clone(), window.state::<CompactState>(), false)?;
    let after = check().map_err(|e| e.to_string())?;
    if small.0 != expected_size || small.1 != expected_position || !small.2 || before != after {
        return Err(format!(
            "Geometry mismatch: before={before:?}, small={small:?}, after={after:?}"
        ));
    }
    Ok(format!("compact={small:?}; restored={after:?}"))
}
