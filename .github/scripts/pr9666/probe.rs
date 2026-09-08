// SPDX-License-Identifier: AGPL-3.0-only
// Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.
use serde_json::{json, Value};
use std::{
    ffi::{c_char, c_void, CStr},
    path::PathBuf,
};
use tauri::Manager;

extern "C" {
    fn pr9666_geometry(view: *mut c_void) -> *mut c_char;
    fn pr9666_free(pointer: *mut c_char);
}

fn output() -> PathBuf {
    PathBuf::from(std::env::var("PR9666_OUTPUT").expect("PR9666_OUTPUT required"))
}

#[tauri::command]
async fn native_geometry(window: tauri::WebviewWindow) -> Result<Value, String> {
    let (tx, rx) = tokio::sync::oneshot::channel();
    window
        .with_webview(move |view| {
            let result = unsafe {
                let pointer = pr9666_geometry(view.inner());
                if pointer.is_null() {
                    Err("Native window unavailable".to_string())
                } else {
                    let parsed = serde_json::from_slice(CStr::from_ptr(pointer).to_bytes())
                        .map_err(|e| e.to_string());
                    pr9666_free(pointer);
                    parsed
                }
            };
            let _ = tx.send(result);
        })
        .map_err(|e| e.to_string())?;
    tokio::time::timeout(std::time::Duration::from_secs(10), rx)
        .await
        .map_err(|_| "Native geometry timeout")?
        .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn capture(
    window: tauri::WebviewWindow,
    label: String,
    facts: Value,
) -> Result<Value, String> {
    if label.is_empty()
        || !label
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || c == b'-')
    {
        return Err("Invalid capture label".into());
    }
    let native = native_geometry(window).await?;
    let number = native["window_number"]
        .as_i64()
        .ok_or("Missing native window number")?;
    let path = output().join(format!("{label}.png"));
    let saved_path = path.clone();
    let capture_result = tokio::task::spawn_blocking(move || {
        std::process::Command::new("/usr/sbin/screencapture")
            .args(["-x", "-o", "-l", &number.to_string()])
            .arg(path)
            .output()
    })
    .await
    .map_err(|e| e.to_string())?
    .map_err(|e| e.to_string())?;
    let status = capture_result.status.success() && saved_path.is_file();
    let record = json!({"label": label, "facts": facts, "native": native,
        "capture_ok": status, "capture_error": String::from_utf8_lossy(&capture_result.stderr),
        "image": format!("{label}.png")});
    std::fs::write(
        output().join(format!("{label}.json")),
        serde_json::to_vec_pretty(&record).unwrap(),
    )
    .map_err(|e| e.to_string())?;
    if !status {
        return Err("Native window screenshot unavailable; not valid visual evidence".into());
    }
    Ok(record)
}

#[tauri::command]
fn finish(app: tauri::AppHandle, result: Value) -> Result<(), String> {
    std::fs::write(
        output().join("result.json"),
        serde_json::to_vec_pretty(&result).unwrap(),
    )
    .map_err(|e| e.to_string())?;
    // Let the command reply settle before closing the process.
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(std::time::Duration::from_millis(200)).await;
        app.exit(0);
    });
    Ok(())
}

fn main() {
    std::fs::create_dir_all(output()).unwrap();
    let mut context = tauri::generate_context!();
    context.config_mut().build.dev_url = Some(
        std::env::var("PR9666_URL")
            .expect("PR9666_URL required")
            .parse()
            .expect("Invalid loopback URL"),
    );
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![native_geometry, capture, finish])
        .setup(|app| {
            let window = app
                .get_webview_window("main")
                .ok_or("main window unavailable")?;
            window.show()?;
            window.set_focus()?;
            Ok(())
        })
        .run(context)
        .expect("Native test window failed");
}
