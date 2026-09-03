// SPDX-License-Identifier: AGPL-3.0-only
// Copyright 2026-present the Unsloth AI Inc. team. All rights reserved.

import assert from "node:assert/strict";
import test from "node:test";

import { registerBundlerResolver } from "./helpers/kit.ts";

registerBundlerResolver();

const { classifyGgufFit } = await import("../src/lib/gguf-fit.ts");
const variantSortModule: Record<string, unknown> = await import(
  "../src/features/hub/lib/gguf-variant-sort.ts"
);

const GB = 1000 ** 3;

type VariantFitClassifier = (
  variant: { size_bytes: number; download_size_bytes?: number | null },
  resources: { gpuGb: number; systemRamGb: number },
) => "fits" | "marginal" | "partial" | "ram" | "oom";

test("hub fit includes Muse Glimmer companion GGUFs", () => {
  const variant = {
    size_bytes: 12_789_199_648,
    download_size_bytes: 18_089_467_744,
  };
  const resources = {
    gpuGb: 16,
    systemRamGb: 32,
  };
  const classifyVariantFit = variantSortModule["classifyGgufVariantFit"];

  // This fallback is the pre-fix Hub call: it classified only the main model file.
  // Keeping both paths in one probe lets the exact same CI test run before and after.
  const actual =
    typeof classifyVariantFit === "function"
      ? (classifyVariantFit as VariantFitClassifier)(variant, resources)
      : classifyGgufFit(variant.size_bytes, resources);

  console.log(
    JSON.stringify({
      classifier: typeof classifyVariantFit === "function" ? "full-variant" : "main-file-only",
      mainGB: variant.size_bytes / GB,
      fullVariantGB: variant.download_size_bytes / GB,
      actual,
      expected: "partial",
    }),
  );
  assert.equal(actual, "partial");
});
