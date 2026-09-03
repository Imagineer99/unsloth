# SPDX-License-Identifier: AGPL-3.0-only
# Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.

"""Pinned A/B probe for Unsloth PR #10224.

This file is byte-identical on the base and head staging branches. Assertions
describe the corrected behavior, so the base run must fail and the head run
must pass for the intended reason.
"""

from __future__ import annotations

import json
import os
import platform
import sys
import types
from contextlib import contextmanager

sys.path.insert(0, os.path.join(os.getcwd(), "studio", "backend"))

import mlx.core as mx
import psutil

import utils.hardware.hardware as hardware
from utils.hardware.hardware import DeviceType


GIB = 1024**3
failures: list[str] = []


def check(label: str, condition: bool, detail: str) -> None:
    outcome = "PASS" if condition else "FAIL"
    print(f"{outcome} {label}: {detail}")
    if not condition:
        failures.append(label)


def near(actual: float, expected: float, tolerance: float = 0.01) -> bool:
    return abs(actual - expected) <= tolerance


@contextmanager
def simulated_apple(*, total_gib: float, available_gib: float, allocated_gib: float, recommended_gib: float):
    original_get_device = hardware.get_device
    original_read_stats = hardware._read_apple_gpu_stats
    original_virtual_memory = psutil.virtual_memory
    original_device_info = mx.device_info
    try:
        hardware.get_device = lambda: DeviceType.MLX
        hardware._read_apple_gpu_stats = lambda: {
            "vram_used_bytes": int(allocated_gib * GIB),
        }
        psutil.virtual_memory = lambda: types.SimpleNamespace(
            total=int(total_gib * GIB),
            available=int(available_gib * GIB),
        )
        mx.device_info = lambda: {
            "device_name": "Apple A/B fixture",
            "max_recommended_working_set_size": int(recommended_gib * GIB),
        }
        yield
    finally:
        hardware.get_device = original_get_device
        hardware._read_apple_gpu_stats = original_read_stats
        psutil.virtual_memory = original_virtual_memory
        mx.device_info = original_device_info


def simulated_case(label: str, *, total: float, available: float, allocated: float, recommended: float, expected: float) -> None:
    with simulated_apple(
        total_gib=total,
        available_gib=available,
        allocated_gib=allocated,
        recommended_gib=recommended,
    ):
        info = hardware.get_gpu_memory_info()
    actual = info["free_gb"]
    check(
        label,
        near(actual, expected),
        f"reported={actual:.2f} GiB expected={expected:.2f} GiB "
        f"(total={total}, available={available}, allocated={allocated}, recommended={recommended})",
    )


print(f"machine={platform.system()} {platform.machine()} macOS={platform.mac_ver()[0]}")
print(f"python={sys.version.split()[0]}")
real_vm = psutil.virtual_memory()
real_device = mx.device_info()
hardware.get_device = lambda: DeviceType.MLX
real_info = hardware.get_gpu_memory_info()
print("REAL_MAC_INFO=" + json.dumps({
    "psutil_total_gib": real_vm.total / GIB,
    "psutil_available_gib": real_vm.available / GIB,
    "device_name": real_device.get("device_name"),
    "recommended_gib": (real_device.get("max_recommended_working_set_size") or 0) / GIB,
    "reported": real_info,
}, sort_keys=True))

simulated_case(
    "host-available bound",
    total=16,
    available=6,
    allocated=1.2,
    recommended=11,
    expected=6,
)
simulated_case(
    "remaining Metal headroom",
    total=16,
    available=10,
    allocated=8,
    recommended=11,
    expected=3,
)
simulated_case(
    "exhausted Metal headroom",
    total=16,
    available=6,
    allocated=12,
    recommended=11,
    expected=0,
)
simulated_case(
    "missing Metal recommendation falls back to host available",
    total=16,
    available=6,
    allocated=1.2,
    recommended=0,
    expected=6,
)

with simulated_apple(
    total_gib=16,
    available_gib=6,
    allocated_gib=1.2,
    recommended_gib=11,
):
    summary = hardware.get_gpu_memory_info()
    row = hardware.get_visible_gpu_utilization()["devices"][0]
check(
    "Resources device publishes the same free value",
    row.get("vram_free_gb") is not None
    and near(row["vram_free_gb"], summary["free_gb"]),
    f"device={row.get('vram_free_gb')} summary={summary['free_gb']}",
)

original_get_device = hardware.get_device
try:
    hardware.get_device = lambda: DeviceType.CPU
    cpu_info = hardware.get_gpu_memory_info()
finally:
    hardware.get_device = original_get_device
check(
    "CPU fallback remains unchanged",
    cpu_info == {"available": False, "backend": "cpu"},
    repr(cpu_info),
)

if failures:
    print(f"AB_RESULT=DEFECT_PRESENT failures={len(failures)}")
    for failure in failures:
        print(f"  - {failure}")
    raise SystemExit(1)

print("AB_RESULT=CORRECTED")
