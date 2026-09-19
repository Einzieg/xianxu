#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod engine;
mod window_mode;

use engine::Engine;
use serde_json::{json, Value};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use tauri::{Emitter, Manager};

struct HostState {
    engine: Arc<Engine>,
    closing: AtomicBool,
    allow_exit: AtomicBool,
    smoke_started: AtomicBool,
}

#[tauri::command]
async fn rpc(request: Value, state: tauri::State<'_, HostState>) -> Result<Value, String> {
    if state.closing.load(Ordering::SeqCst) {
        return Err("The player is shutting down; new engine requests are disabled.".into());
    }
    let engine = Arc::clone(&state.engine);
    tauri::async_runtime::spawn_blocking(move || {
        let result = engine.request(request);
        if let Err(error) = &result {
            engine.log(error);
        }
        result
    })
    .await
    .map_err(|error| format!("Engine worker failed: {error}"))?
}

fn begin_shutdown(app: tauri::AppHandle) {
    let state = app.state::<HostState>();
    if state.closing.swap(true, Ordering::SeqCst) {
        return;
    }
    let engine = Arc::clone(&state.engine);
    let _ = app.emit("engine-shutdown-started", ());
    tauri::async_runtime::spawn(async move {
        let result = tauri::async_runtime::spawn_blocking(move || engine.shutdown())
            .await
            .map_err(|error| format!("Shutdown worker failed: {error}"))
            .and_then(|result| result);
        let state = app.state::<HostState>();
        match result {
            Ok(_) => {
                state
                    .engine
                    .log("Shutdown acknowledged; Python exited normally.");
                state.allow_exit.store(true, Ordering::SeqCst);
                app.exit(0);
            }
            Err(error) => {
                state.engine.log(&format!("Shutdown refused: {error}"));
                state.closing.store(false, Ordering::SeqCst);
                let _ = app.emit("engine-shutdown-error", json!({ "message": error }));
            }
        }
    });
}

fn run_smoke(app: tauri::AppHandle, mut frontend_ok: bool) {
    let engine = Arc::clone(&app.state::<HostState>().engine);
    let test_app = app.clone();
    if cfg!(feature = "custom-protocol") {
        match app.asset_resolver().get("index.html".into()) {
            Some(asset) if asset.mime_type() == "text/html" && !asset.bytes().is_empty() => {
                engine.log(&format!(
                    "SMOKE embedded index.html resolved ({} bytes).",
                    asset.bytes().len()
                ));
            }
            _ => {
                engine.log("SMOKE embedded index.html is missing or invalid.");
                frontend_ok = false;
            }
        }
    }
    tauri::async_runtime::spawn(async move {
        let result = tauri::async_runtime::spawn_blocking(move || {
            if std::env::var("ROCK_MUSIC_WINDOW_SMOKE").as_deref() == Ok("1") {
                let result = test_app
                    .get_webview_window("main")
                    .ok_or_else(|| "Missing main window".to_string())
                    .and_then(|window| window_mode::smoke(&window));
                match result {
                    Ok(message) => engine.log(&format!("SMOKE window modes verified: {message}")),
                    Err(error) => {
                        engine.log(&format!("SMOKE window modes failed: {error}"));
                        frontend_ok = false;
                    }
                }
            }
            let state = engine.request(json!({ "method": "get_state", "params": {} }));
            match &state {
                Ok(_) => engine.log("SMOKE get_state succeeded (no playback requested)."),
                Err(error) => engine.log(&format!("SMOKE get_state failed: {error}")),
            }
            // Even a failed state request must go through safe shutdown, never process::exit.
            engine.shutdown().map(|_| state.is_ok() && frontend_ok)
        })
        .await
        .map_err(|error| format!("Smoke worker failed: {error}"))
        .and_then(|result| result);
        let state = app.state::<HostState>();
        match result {
            Ok(passed) => {
                state
                    .engine
                    .log("SMOKE shutdown acknowledged; Python exited normally.");
                state.allow_exit.store(true, Ordering::SeqCst);
                app.exit(if passed { 0 } else { 1 });
            }
            Err(error) => {
                state
                    .engine
                    .log(&format!("SMOKE shutdown failed; host stays open: {error}"));
                state.closing.store(false, Ordering::SeqCst);
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.show();
                }
                let _ = app.emit("engine-shutdown-error", json!({ "message": error }));
            }
        }
    });
}

fn smoke_frontend(webview: &tauri::Webview) {
    let app = webview.app_handle().clone();
    let state = app.state::<HostState>();
    if state.smoke_started.swap(true, Ordering::SeqCst) {
        return;
    }
    let webview = webview.clone();
    tauri::async_runtime::spawn_blocking(move || {
        // React can commit just after WebView2's navigation-finished notification.
        std::thread::sleep(std::time::Duration::from_millis(250));
        let callback_app = app.clone();
        let result = webview.eval_with_callback(
            "({url: location.href, ready: document.readyState, rootChildren: document.getElementById('root')?.childElementCount ?? 0, embedded: location.hostname === 'tauri.localhost'})",
            move |result| {
                callback_app.state::<HostState>().engine.log(&format!("SMOKE WebView DOM: {result}"));
                let passed = serde_json::from_str::<Value>(&result)
                    .map(|value| value["embedded"] == true && value["rootChildren"].as_u64().unwrap_or(0) > 0)
                    .unwrap_or(false);
                run_smoke(callback_app.clone(), passed);
            },
        );
        if let Err(error) = result {
            app.state::<HostState>()
                .engine
                .log(&format!("SMOKE WebView evaluation failed: {error}"));
            run_smoke(app, false);
        }
    });
}

fn main() {
    let app = tauri::Builder::default()
        .manage(window_mode::CompactState::default())
        .plugin(tauri_plugin_single_instance::init(|app, _, _| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.unminimize();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            let data_dir =
                match std::env::var_os("ROCK_MUSIC_DATA_DIR").filter(|path| !path.is_empty()) {
                    Some(path) => std::path::PathBuf::from(path),
                    None => app.path().app_data_dir()?,
                };
            std::fs::create_dir_all(&data_dir)?;
            let data_dir = std::fs::canonicalize(data_dir)?;
            let smoke = std::env::var("ROCK_MUSIC_SMOKE").as_deref() == Ok("1");
            app.manage(HostState {
                engine: Arc::new(Engine::new(data_dir)),
                closing: AtomicBool::new(smoke),
                allow_exit: AtomicBool::new(false),
                smoke_started: AtomicBool::new(false),
            });
            if smoke {
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.hide();
                }
                if !cfg!(feature = "custom-protocol") {
                    run_smoke(app.handle().clone(), true);
                }
            }
            Ok(())
        })
        .on_page_load(|webview, payload| {
            if cfg!(feature = "custom-protocol")
                && std::env::var("ROCK_MUSIC_SMOKE").as_deref() == Ok("1")
                && matches!(payload.event(), tauri::webview::PageLoadEvent::Finished)
            {
                smoke_frontend(webview);
            }
        })
        .invoke_handler(tauri::generate_handler![rpc, window_mode::set_compact])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                if !window
                    .state::<HostState>()
                    .allow_exit
                    .load(Ordering::SeqCst)
                {
                    api.prevent_close();
                    begin_shutdown(window.app_handle().clone());
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("Failed to initialize the Tauri WebView2 host");

    app.run(|app, event| {
        if let tauri::RunEvent::ExitRequested { api, .. } = event {
            if !app.state::<HostState>().allow_exit.load(Ordering::SeqCst) {
                api.prevent_exit();
                begin_shutdown(app.clone());
            }
        }
    });
}
