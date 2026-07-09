// SPDX-License-Identifier: AGPL-3.0-only
// Copyright 2026-present the Unsloth AI Inc. team. All rights reserved. See /studio/LICENSE.AGPL-3.0

import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useChatRuntimeStore } from "@/features/chat";
import {
  useMonitorOverlayStore,
  useSettingsDialogStore,
} from "@/features/settings";
import { type SystemInfoResponse, useSystemInfo } from "@/hooks/use-system";
import { useT } from "@/i18n";
import { cn } from "@/lib/utils";
import { CpuIcon, GripVerticalIcon, XIcon } from "lucide-react";
import {
  AnimatePresence,
  motion,
  useDragControls,
  useMotionValue,
} from "motion/react";
import {
  type PointerEvent,
  type RefObject,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

function clampPercent(value: number): number {
  return Math.max(0, Math.min(100, value));
}

function formatPercent(value: number): string {
  return `${Math.round(clampPercent(value))}%`;
}

function usageIndicatorClass(percent: number): string {
  if (percent >= 90) {
    return "bg-destructive";
  }
  if (percent >= 70) {
    return "bg-amber-500";
  }
  return "bg-primary";
}

function usageTextClass(percent: number): string {
  if (percent >= 90) {
    return "text-destructive";
  }
  if (percent >= 70) {
    return "text-amber-600 dark:text-amber-400";
  }
  return "text-primary";
}

function formatGiB(value: number): string {
  // RAM/VRAM come from the backend in binary units (bytes / 1024**3), matching
  // nvidia-smi and PyTorch, so label the readout GiB rather than GB.
  const digits = value >= 10 ? 1 : 2;
  return `${value.toFixed(digits)} GiB`;
}

function getRunSettingsPanelRect(): DOMRect | null {
  if (typeof window === "undefined") {
    return null;
  }

  const panel = document.querySelector<HTMLElement>(
    '[data-tour="chat-settings"]',
  );
  const rect = panel?.getBoundingClientRect();
  if (!rect || rect.width <= 0 || rect.left >= window.innerWidth) {
    return null;
  }

  return rect;
}

function useRunSettingsCollisionAvoidance(
  enabled: boolean,
  monitorRef: RefObject<HTMLElement | null>,
) {
  const x = useMotionValue(0);
  const autoShiftRef = useRef(0);

  useEffect(() => {
    if (!enabled) {
      const autoShift = autoShiftRef.current;
      if (autoShift !== 0) {
        x.set(x.get() + autoShift);
        autoShiftRef.current = 0;
      }
      return;
    }

    let frameId: number | null = null;
    let intervalId: number | null = null;
    const panel = document.querySelector<HTMLElement>(
      '[data-tour="chat-settings"]',
    );

    const update = () => {
      const panelRect = getRunSettingsPanelRect();
      const monitorRect = monitorRef.current?.getBoundingClientRect();
      if (!(panelRect && monitorRect)) {
        return;
      }

      const gap = 16;
      const overlap = monitorRect.right - panelRect.left + gap;
      if (overlap > 0) {
        x.set(x.get() - overlap);
        autoShiftRef.current += overlap;
        return;
      }

      const slack = panelRect.left - gap - monitorRect.right;
      if (slack > 0 && autoShiftRef.current > 0) {
        const restore = Math.min(slack, autoShiftRef.current);
        x.set(x.get() + restore);
        autoShiftRef.current -= restore;
      }
    };

    const scheduleUpdate = () => {
      if (frameId !== null) {
        return;
      }
      frameId = window.requestAnimationFrame(() => {
        frameId = null;
        update();
      });
    };

    update();
    window.addEventListener("resize", scheduleUpdate);

    let observer: ResizeObserver | null = null;
    if (panel && typeof ResizeObserver !== "undefined") {
      observer = new ResizeObserver(scheduleUpdate);
      observer.observe(panel);
    }

    intervalId = window.setInterval(scheduleUpdate, 50);
    const timeoutId = window.setTimeout(() => {
      if (intervalId !== null) {
        window.clearInterval(intervalId);
        intervalId = null;
      }
    }, 350);

    return () => {
      window.removeEventListener("resize", scheduleUpdate);
      observer?.disconnect();
      window.clearTimeout(timeoutId);
      if (intervalId !== null) {
        window.clearInterval(intervalId);
      }
      if (frameId !== null) {
        window.cancelAnimationFrame(frameId);
      }
    };
  }, [enabled, monitorRef, x]);

  return {
    x,
    clearAutoShift: () => {
      autoShiftRef.current = 0;
    },
  };
}

function getResourceMetrics(systemInfo: SystemInfoResponse) {
  const ramTotal = systemInfo.memory?.total_gb ?? 0;
  const ramAvailable = systemInfo.memory?.available_gb ?? 0;
  const ramUsed = Math.max(0, ramTotal - ramAvailable);
  const ramPercent = clampPercent(systemInfo.memory?.percent_used ?? 0);

  const devices = systemInfo.gpu?.devices ?? [];
  const vramTotal = devices.reduce(
    (sum, device) => sum + (device.memory_total_gb ?? 0),
    0,
  );
  const vramUsed = devices.reduce(
    (sum, device) => sum + (device.vram_used_gb ?? 0),
    0,
  );
  const vramPercent = clampPercent(
    vramTotal > 0 ? (vramUsed / vramTotal) * 100 : 0,
  );
  const hasGpu = (systemInfo.gpu?.available ?? false) && devices.length > 0;

  return {
    ramTotal,
    ramAvailable,
    ramUsed,
    ramPercent,
    devices,
    vramTotal,
    vramUsed,
    vramPercent,
    hasGpu,
  };
}

export function LiveMonitorStatusChip({
  className,
}: { className?: string } = {}) {
  const t = useT();
  const { isOpen } = useMonitorOverlayStore();
  const systemInfo = useSystemInfo({ enabled: isOpen, pollMs: 5000 });

  if (!isOpen) {
    return null;
  }

  const metrics = getResourceMetrics(systemInfo);
  const ramLabel = t("settings.resources.liveMonitor.ram");
  const vramLabel = t("settings.resources.liveMonitor.vram");
  const statusLabel = metrics.hasGpu
    ? `${ramLabel}: ${formatPercent(metrics.ramPercent)} | ${vramLabel}: ${formatPercent(metrics.vramPercent)}`
    : `${ramLabel}: ${formatPercent(metrics.ramPercent)}`;

  return (
    <Tooltip>
      <TooltipTrigger asChild={true}>
        <button
          type="button"
          aria-label={statusLabel}
          className={cn(
            "flex h-[var(--studio-chat-control-height,34px)] items-center gap-2 rounded-[10px] px-2.5 py-1 font-mono text-[13px] tabular-nums text-chat-icon-fg transition-colors hover:bg-chat-icon-bg-hover hover:text-chat-icon-fg-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
            className,
          )}
        >
          <span className={cn("shrink-0", usageTextClass(metrics.ramPercent))}>
            {ramLabel}: {formatPercent(metrics.ramPercent)}
          </span>
          {metrics.hasGpu ? (
            <>
              <span
                className="shrink-0 text-muted-foreground/60"
                aria-hidden={true}
              >
                |
              </span>
              <span
                className={cn("shrink-0", usageTextClass(metrics.vramPercent))}
              >
                {vramLabel}: {formatPercent(metrics.vramPercent)}
              </span>
            </>
          ) : null}
        </button>
      </TooltipTrigger>
      <TooltipContent
        side="bottom"
        sideOffset={8}
        variant="rich"
        className="[&_span>svg]:hidden!"
      >
        <div className="grid min-w-48 gap-1.5 text-xs">
          <div className="flex items-center justify-between gap-4">
            <span className="text-muted-foreground">{ramLabel}</span>
            <span
              className={cn(
                "font-mono font-medium tabular-nums",
                usageTextClass(metrics.ramPercent),
              )}
            >
              {formatPercent(metrics.ramPercent)}
            </span>
          </div>
          <div className="flex items-center justify-between gap-4">
            <span className="text-muted-foreground">
              {t("settings.resources.liveMonitor.free", {
                value: formatGiB(metrics.ramAvailable),
              })}
            </span>
            <span className="font-mono tabular-nums">
              {formatGiB(metrics.ramUsed)} / {formatGiB(metrics.ramTotal)}
            </span>
          </div>
          <div className="my-0.5 border-t border-border/40" />
          {metrics.hasGpu ? (
            <>
              <div className="flex items-center justify-between gap-4">
                <span className="text-muted-foreground">{vramLabel}</span>
                <span
                  className={cn(
                    "font-mono font-medium tabular-nums",
                    usageTextClass(metrics.vramPercent),
                  )}
                >
                  {formatPercent(metrics.vramPercent)}
                </span>
              </div>
              <div className="flex items-center justify-between gap-4">
                <span className="text-muted-foreground">
                  {metrics.devices.length > 1
                    ? `${metrics.devices.length} GPUs`
                    : (metrics.devices[0].name ?? "GPU")}
                </span>
                <span className="font-mono tabular-nums">
                  {formatGiB(metrics.vramUsed)} / {formatGiB(metrics.vramTotal)}
                </span>
              </div>
            </>
          ) : (
            <div className="text-xs text-muted-foreground">
              {t("settings.resources.liveMonitor.noGpu")}
            </div>
          )}
        </div>
      </TooltipContent>
    </Tooltip>
  );
}

export function FloatingMonitor() {
  const t = useT();
  const { isOpen, setIsOpen } = useMonitorOverlayStore();
  const settingsPanelOpen = useChatRuntimeStore((s) => s.settingsPanelOpen);
  const settingsDialogOpen = useSettingsDialogStore((s) => s.open);
  const dockedByRunSettings = settingsPanelOpen && !settingsDialogOpen;
  const systemInfo = useSystemInfo({ enabled: isOpen, pollMs: 5000 });

  const [constraintsElement, setConstraintsElement] =
    useState<HTMLDivElement | null>(null);
  const constraintsRef = useMemo(
    () => ({ current: constraintsElement }),
    [constraintsElement],
  );
  const dragControls = useDragControls();
  const monitorRef = useRef<HTMLDivElement>(null);
  const monitorCollision = useRunSettingsCollisionAvoidance(
    dockedByRunSettings,
    monitorRef,
  );

  function startDrag(event: PointerEvent<HTMLDivElement>) {
    event.preventDefault();
    monitorCollision.clearAutoShift();
    dragControls.start(event);
  }

  const metrics = getResourceMetrics(systemInfo);

  return (
    <AnimatePresence>
      {isOpen && (
        <div
          ref={setConstraintsElement}
          className="fixed inset-0 z-[70] pointer-events-none"
        >
          <motion.div
            ref={monitorRef}
            drag={true}
            dragControls={dragControls}
            dragListener={false}
            dragConstraints={constraintsRef}
            dragElastic={0}
            dragMomentum={false}
            initial={{ opacity: 0, scale: 0.9 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0, scale: 0.9 }}
            className="settings-surface fixed bottom-4 right-4 w-64 max-w-[calc(100vw-2rem)] resize overflow-hidden rounded-xl border border-border/70 p-3 shadow-border ring-0 backdrop-blur-sm pointer-events-auto cursor-default select-none"
            style={{ x: monitorCollision.x }}
          >
            <div className="mb-2 flex items-center justify-between gap-2 border-b border-border/60 pb-2">
              <div className="flex min-w-0 flex-1 items-center gap-1.5 truncate text-xs font-semibold text-foreground">
                <CpuIcon className="size-3.5 shrink-0 text-primary" />
                <span className="truncate">
                  {t("settings.resources.liveMonitor.title")}
                </span>
              </div>
              <div className="flex items-center gap-1 shrink-0">
                <div
                  onPointerDown={startDrag}
                  className="touch-none cursor-grab rounded-md px-1 text-muted-foreground/60 transition-colors hover:bg-muted/60 hover:text-muted-foreground active:cursor-grabbing"
                >
                  <GripVerticalIcon className="size-3.5" />
                </div>

                <Button
                  size="icon-xs"
                  variant="ghost"
                  className="text-muted-foreground hover:text-foreground"
                  onClick={() => setIsOpen(false)}
                  title={t("common.close")}
                  aria-label={t("common.close")}
                >
                  <XIcon className="size-3" />
                </Button>
              </div>
            </div>

            <motion.div
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: "auto" }}
              exit={{ opacity: 0, height: 0 }}
              className="space-y-3 overflow-hidden"
            >
              <div className="space-y-1">
                <div className="flex justify-between text-[11px] font-medium font-mono">
                  <span>{t("settings.resources.liveMonitor.ram")}</span>
                  <span
                    className={cn(
                      "tabular-nums",
                      usageTextClass(metrics.ramPercent),
                    )}
                  >
                    {formatPercent(metrics.ramPercent)}
                  </span>
                </div>
                <div className="text-xs text-muted-foreground font-mono tabular-nums">
                  {formatGiB(metrics.ramUsed)} / {formatGiB(metrics.ramTotal)}
                </div>
                <Progress
                  value={metrics.ramPercent}
                  className="mt-1 h-1.5 rounded-full bg-muted"
                  indicatorClassName={usageIndicatorClass(metrics.ramPercent)}
                />
              </div>

              {metrics.hasGpu && (
                <div className="space-y-1">
                  <div className="flex justify-between text-[11px] font-medium font-mono">
                    <span className="truncate flex-1 pr-2">
                      {t("settings.resources.liveMonitor.vram")} {" "}
                      {metrics.devices.length > 1
                        ? `(${metrics.devices.length} GPUs)`
                        : `(${metrics.devices[0].name ?? "GPU"})`}
                    </span>
                    <span
                      className={cn(
                        "shrink-0 tabular-nums",
                        usageTextClass(metrics.vramPercent),
                      )}
                    >
                      {formatPercent(metrics.vramPercent)}
                    </span>
                  </div>
                  <div className="text-xs text-muted-foreground font-mono tabular-nums">
                    {formatGiB(metrics.vramUsed)} / {formatGiB(metrics.vramTotal)}
                  </div>
                  <Progress
                    value={metrics.vramPercent}
                    className="mt-1 h-1.5 rounded-full bg-muted"
                    indicatorClassName={usageIndicatorClass(metrics.vramPercent)}
                  />
                </div>
              )}
            </motion.div>
          </motion.div>
        </div>
      )}
    </AnimatePresence>
  );
}
