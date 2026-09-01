#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.
"""Instrument a disposable Windows build for PR 9505 tray-restore proof."""

from __future__ import annotations

import argparse
from pathlib import Path


PERIODIC_DECLARATION = "const PERIODIC_UPDATE_CHECK_INTERVAL_MS = 60 * 60 * 1000;"
REPRO_PERIODIC_DECLARATION = (
    "const PERIODIC_UPDATE_CHECK_INTERVAL_MS = 2_000; // PR 9505 repro only"
)
PERIODIC_TIMER_NEEDLE = """    const periodicTimer = setInterval(() => {
      scheduledCheckRef.current();
    }, PERIODIC_UPDATE_CHECK_INTERVAL_MS);"""
REPRO_PERIODIC_TIMER = """    const periodicTimer = setInterval(() => {
      scheduledCheckRef.current();
    }, 60_000); // PR 9505 repro: keep the interval dormant during restore proof"""
RESTORE_HANDLER_NEEDLE = "const checkWhenVisibleAndDue = () => {"
VISIBLE_DECLARATION = '"visible": false,'
REPRO_VISIBLE_DECLARATION = '"visible": true,'
PROBE_COMPONENT_NEEDLE = "export function AppProvider({ children }: AppProviderProps) {"
PROBE_COMPONENT_REPLACEMENT = """function Pr9505ReproUpdateProbe() {
  useTauriUpdate(false);
  return null;
}

export function AppProvider({ children }: AppProviderProps) {"""
PROBE_MOUNT_NEEDLE = """      <TooltipProvider>
        <AppearanceCustomizationEffect />"""
PROBE_MOUNT_REPLACEMENT = """      <TooltipProvider>
        <Pr9505ReproUpdateProbe />
        <AppearanceCustomizationEffect />"""

IMPORT_NEEDLE = "use serde::{Deserialize, Serialize};\n"
IMPORTS = """use serde::{Deserialize, Serialize};
#[cfg(windows)]
use std::{
    sync::atomic::{AtomicBool, AtomicUsize, Ordering},
    time::Duration,
};
#[cfg(windows)]
use webview2_com::{
    Microsoft::Web::WebView2::Win32::ICoreWebView2_3,
    TrySuspendCompletedHandler,
};
#[cfg(windows)]
use windows_core::{Interface, BOOL};
"""

STRUCT_NEEDLE = """pub(crate) struct DesktopUpdateMetadata {
    current_version: String,
    version: String,
    date: Option<String>,
    body: Option<String>,
    raw_json: serde_json::Value,
}
"""

HARNESS = r"""

#[cfg(windows)]
static PR9505_REPRO_STARTED: AtomicBool = AtomicBool::new(false);
#[cfg(windows)]
static PR9505_REPRO_CHECKS: AtomicUsize = AtomicUsize::new(0);

#[cfg(windows)]
fn pr9505_repro_start(webview: tauri::Webview) {
    if PR9505_REPRO_STARTED.swap(true, Ordering::SeqCst) {
        return;
    }

    tauri::async_runtime::spawn(async move {
        // Return the first update check before suspending the renderer.
        tokio::time::sleep(Duration::from_millis(250)).await;
        let app = webview.app_handle().clone();
        let Some(window) = app.get_webview_window("main") else {
            eprintln!("PR9505_REPRO HARNESS_ERROR missing main window");
            std::process::exit(18);
        };
        if let Err(error) = window.hide() {
            eprintln!("PR9505_REPRO HARNESS_ERROR hide failed: {error}");
            std::process::exit(18);
        }

        let (suspend_tx, suspend_rx) = tokio::sync::oneshot::channel();
        if let Err(error) = window.with_webview(move |platform| {
            let controller = platform.controller();
            if let Err(error) = unsafe { controller.SetIsVisible(false) } {
                eprintln!("PR9505_REPRO HARNESS_ERROR SetIsVisible failed: {error}");
                std::process::exit(18);
            }
            let core = match unsafe { controller.CoreWebView2() } {
                Ok(core) => core,
                Err(error) => {
                    eprintln!("PR9505_REPRO HARNESS_ERROR CoreWebView2 failed: {error}");
                    std::process::exit(18);
                }
            };
            let core3: ICoreWebView2_3 = match core.cast() {
                Ok(core3) => core3,
                Err(error) => {
                    eprintln!("PR9505_REPRO HARNESS_ERROR ICoreWebView2_3 unavailable: {error}");
                    std::process::exit(18);
                }
            };
            let handler = TrySuspendCompletedHandler::create(Box::new(
                move |operation_result, success| {
                    let result = operation_result
                        .map(|()| success)
                        .map_err(|error| error.to_string());
                    let _ = suspend_tx.send(result);
                    Ok(())
                },
            ));
            if let Err(error) = unsafe { core3.TrySuspend(&handler) } {
                eprintln!("PR9505_REPRO HARNESS_ERROR TrySuspend failed: {error}");
                std::process::exit(18);
            }
        }) {
            eprintln!("PR9505_REPRO HARNESS_ERROR with_webview failed: {error}");
            std::process::exit(18);
        }

        let callback_suspended = match tokio::time::timeout(Duration::from_secs(10), suspend_rx).await
        {
            Ok(Ok(Ok(success))) => success,
            Ok(Ok(Err(error))) => {
                eprintln!("PR9505_REPRO HARNESS_ERROR suspend callback failed: {error}");
                std::process::exit(18);
            }
            Ok(Err(error)) => {
                eprintln!("PR9505_REPRO HARNESS_ERROR suspend callback dropped: {error}");
                std::process::exit(18);
            }
            Err(_) => {
                eprintln!("PR9505_REPRO HARNESS_ERROR suspend callback timed out");
                std::process::exit(18);
            }
        };
        if !callback_suspended {
            eprintln!("PR9505_REPRO HARNESS_ERROR WebView2 refused suspension");
            std::process::exit(18);
        }

        let (verify_tx, verify_rx) = tokio::sync::oneshot::channel();
        if let Err(error) = window.with_webview(move |platform| {
            let result = (|| {
                let controller = platform.controller();
                let core = unsafe { controller.CoreWebView2()? };
                let core3: ICoreWebView2_3 = core.cast()?;
                let mut value = BOOL::default();
                unsafe { core3.IsSuspended(&mut value)? };
                Ok::<bool, windows_core::Error>(value.as_bool())
            })()
            .map_err(|error| error.to_string());
            let _ = verify_tx.send(result);
        }) {
            eprintln!("PR9505_REPRO HARNESS_ERROR suspension query failed: {error}");
            std::process::exit(18);
        }
        match tokio::time::timeout(Duration::from_secs(5), verify_rx).await {
            Ok(Ok(Ok(true))) => eprintln!("PR9505_REPRO SUSPEND_CONFIRMED"),
            Ok(Ok(Ok(false))) => {
                eprintln!("PR9505_REPRO HARNESS_ERROR IsSuspended returned false");
                std::process::exit(18);
            }
            Ok(Ok(Err(error))) => {
                eprintln!("PR9505_REPRO HARNESS_ERROR IsSuspended failed: {error}");
                std::process::exit(18);
            }
            Ok(Err(error)) => {
                eprintln!("PR9505_REPRO HARNESS_ERROR suspension query dropped: {error}");
                std::process::exit(18);
            }
            Err(_) => {
                eprintln!("PR9505_REPRO HARNESS_ERROR suspension query timed out");
                std::process::exit(18);
            }
        }

        // The renderer's periodic timer is deliberately dormant. Wait until
        // the restore fallback is overdue and prove suspension itself did not
        // run another JavaScript check.
        tokio::time::sleep(Duration::from_secs(8)).await;
        let hidden_checks = PR9505_REPRO_CHECKS.load(Ordering::SeqCst);
        if hidden_checks != 1 {
            eprintln!(
                "PR9505_REPRO HARNESS_ERROR hidden checks={hidden_checks}, expected=1"
            );
            std::process::exit(18);
        }
        eprintln!("PR9505_REPRO HIDDEN_COUNT_CONFIRMED checks={hidden_checks}");

        let (resume_tx, resume_rx) = tokio::sync::oneshot::channel();
        if let Err(error) = window.with_webview(move |platform| {
            let result = (|| {
                let controller = platform.controller();
                let core = unsafe { controller.CoreWebView2()? };
                let core3: ICoreWebView2_3 = core.cast()?;
                unsafe {
                    core3.Resume()?;
                    controller.SetIsVisible(true)?;
                }
                Ok::<(), windows_core::Error>(())
            })()
            .map_err(|error| error.to_string());
            let _ = resume_tx.send(result);
        }) {
            eprintln!("PR9505_REPRO HARNESS_ERROR resume dispatch failed: {error}");
            std::process::exit(18);
        }
        match tokio::time::timeout(Duration::from_secs(5), resume_rx).await {
            Ok(Ok(Ok(()))) => {}
            Ok(Ok(Err(error))) => {
                eprintln!("PR9505_REPRO HARNESS_ERROR resume failed: {error}");
                std::process::exit(18);
            }
            Ok(Err(error)) => {
                eprintln!("PR9505_REPRO HARNESS_ERROR resume callback dropped: {error}");
                std::process::exit(18);
            }
            Err(_) => {
                eprintln!("PR9505_REPRO HARNESS_ERROR resume callback timed out");
                std::process::exit(18);
            }
        }
        if let Err(error) = window.show() {
            eprintln!("PR9505_REPRO HARNESS_ERROR show failed: {error}");
            std::process::exit(18);
        }
        if let Err(error) = window.set_focus() {
            eprintln!("PR9505_REPRO HARNESS_ERROR focus failed: {error}");
            std::process::exit(18);
        }
        eprintln!("PR9505_REPRO RESTORE_TRIGGERED");

        tokio::time::sleep(Duration::from_secs(5)).await;
        let checks = PR9505_REPRO_CHECKS.load(Ordering::SeqCst);
        if checks >= 2 {
            eprintln!("PR9505_REPRO PASS restored update checks={checks}");
            std::process::exit(0);
        }
        eprintln!("PR9505_REPRO FAIL restored update checks={checks}, expected>=2");
        std::process::exit(17);
    });
}
"""

FUNCTION_NEEDLE = """pub(crate) async fn check_desktop_update(
    webview: tauri::Webview,
    state: tauri::State<'_, DesktopUpdateState>,
) -> Result<Option<DesktopUpdateMetadata>, String> {
"""
FUNCTION_REPLACEMENT = (
    FUNCTION_NEEDLE
    + r"""    #[cfg(windows)]
    if std::env::var("UNSLOTH_PR9505_REPRO").as_deref() == Ok("1") {
        let checks = PR9505_REPRO_CHECKS.fetch_add(1, Ordering::SeqCst) + 1;
        eprintln!("PR9505_REPRO UPDATE_CHECK count={checks}");
        if checks == 1 {
            pr9505_repro_start(webview.clone());
        }
        return Ok(None);
    }
"""
)


def replace_once(text: str, needle: str, replacement: str, label: str) -> str:
    count = text.count(needle)
    if count != 1:
        raise SystemExit(f"expected one {label}, found {count}")
    return text.replace(needle, replacement, 1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expect-restore", choices = ("present", "absent"), required = True)
    args = parser.parse_args()

    repo = Path(__file__).resolve().parents[2]
    hook_path = repo / "studio/frontend/src/hooks/use-tauri-update.ts"
    updater_path = repo / "studio/src-tauri/src/desktop_updater.rs"
    config_path = repo / "studio/src-tauri/tauri.conf.json"
    provider_path = repo / "studio/frontend/src/app/provider.tsx"

    hook = hook_path.read_text(encoding = "utf-8")
    periodic_present = PERIODIC_DECLARATION in hook
    if not periodic_present:
        raise SystemExit("periodic interval declaration is missing")
    restore_present = RESTORE_HANDLER_NEEDLE in hook
    if restore_present != (args.expect_restore == "present"):
        raise SystemExit(
            f"restore handler presence={restore_present}, expected={args.expect_restore}"
        )
    hook = replace_once(
        hook,
        PERIODIC_DECLARATION,
        REPRO_PERIODIC_DECLARATION,
        "periodic interval declaration",
    )
    hook = replace_once(
        hook,
        PERIODIC_TIMER_NEEDLE,
        REPRO_PERIODIC_TIMER,
        "periodic timer",
    )
    hook_path.write_text(hook, encoding = "utf-8")

    config = config_path.read_text(encoding = "utf-8")
    config = replace_once(
        config,
        VISIBLE_DECLARATION,
        REPRO_VISIBLE_DECLARATION,
        "initial window visibility declaration",
    )
    config_path.write_text(config, encoding = "utf-8")

    provider = provider_path.read_text(encoding = "utf-8")
    provider = replace_once(
        provider,
        PROBE_COMPONENT_NEEDLE,
        PROBE_COMPONENT_REPLACEMENT,
        "repro update probe component",
    )
    provider = replace_once(
        provider,
        PROBE_MOUNT_NEEDLE,
        PROBE_MOUNT_REPLACEMENT,
        "repro update probe mount",
    )
    provider_path.write_text(provider, encoding = "utf-8")

    updater = updater_path.read_text(encoding = "utf-8")
    if "PR9505_REPRO_STARTED" in updater:
        raise SystemExit("desktop updater is already instrumented")
    updater = replace_once(updater, IMPORT_NEEDLE, IMPORTS, "serde import")
    updater = replace_once(
        updater,
        STRUCT_NEEDLE,
        STRUCT_NEEDLE + HARNESS,
        "update metadata struct",
    )
    updater = replace_once(
        updater,
        FUNCTION_NEEDLE,
        FUNCTION_REPLACEMENT,
        "check_desktop_update function",
    )
    updater_path.write_text(updater, encoding = "utf-8")
    print(
        "PR9505_REPRO PREPARED "
        f"restore_handler_present={str(restore_present).lower()} "
        "restore_due_ms=2000 periodic_timer_ms=60000 "
        "initial_window_visible=true forced_update_hook=true"
    )


if __name__ == "__main__":
    main()
