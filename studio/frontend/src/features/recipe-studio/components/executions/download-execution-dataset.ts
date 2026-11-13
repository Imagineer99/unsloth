// SPDX-License-Identifier: AGPL-3.0-only
// Copyright 2026-present the Unsloth AI Inc. team. All rights reserved. See /studio/LICENSE.AGPL-3.0

import { isTauri } from "@/lib/api-base";
import {
  downloadFile,
  downloadUrlStreaming,
  isDownloadCancelled,
} from "@/lib/native-files";
import { downloadRecipeJobDataset } from "../../api";
import type { RecipeExecutionRecord } from "../../execution-types";
import { hasCompleteLocalDataset } from "../../executions/execution-helpers";

/** "started": a browser anchor click resolves before the request is sent. "partial": rows
 * this client still holds, when the server no longer has the run. */
export type DownloadOutcome = "saved" | "started" | "partial";

function sanitizeFilenameStem(value: string): string {
  const cleaned = value
    .trim()
    .replace(/[^\w.-]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return cleaned || "recipe-dataset";
}

function buildDownloadFilename(execution: RecipeExecutionRecord): string {
  const runName = execution.run_name?.trim();
  if (runName) {
    return sanitizeFilenameStem(runName);
  }
  return sanitizeFilenameStem(execution.id);
}

async function triggerClientJsonlDownload(
  rows: Record<string, unknown>[],
  filenameStem: string,
): Promise<void> {
  const body =
    rows.map((row) => JSON.stringify(row)).join("\n") + (rows.length > 0 ? "\n" : "");
  await downloadFile(body, `${filenameStem}.jsonl`, "application/x-ndjson");
}

export async function downloadExecutionDataset(
  execution: RecipeExecutionRecord,
): Promise<DownloadOutcome> {
  const filenameStem = buildDownloadFilename(execution);

  if (execution.jobId) {
    try {
      const { url, filename } = await downloadRecipeJobDataset(execution.jobId, {
        artifactPath: execution.artifact_path,
        filename: filenameStem,
      });
      await downloadUrlStreaming(url, filename);
      return isTauri ? "saved" : "started";
    } catch (error) {
      if (isDownloadCancelled(error)) {
        throw error;
      }
      // An artifact-backed run keeps images beside the parquet; a bare JSONL would lose them.
      if (execution.artifact_path) {
        throw error;
      }
    }
  }

  if (execution.dataset.length === 0) {
    throw new Error("This run does not have a dataset to download yet.");
  }

  // Older run records may contain only the displayed JPEG for a generated image.
  // Their saved recipe identifies these columns without mistaking ordinary JSON fields for images.
  if (execution.kind === "preview" && execution.recipeSignature) {
    let columns: unknown;
    try {
      columns = JSON.parse(execution.recipeSignature)?.payload?.recipe?.columns;
    } catch {
      // Legacy records without a recipe snapshot can still export their saved rows.
    }
    const hasImageThumbnail =
      Array.isArray(columns) &&
      columns.some(
        (column) =>
          column?.column_type === "image" &&
          execution.dataset.some((row) => {
            const cell = row[column.name];
            return typeof cell === "object" && cell !== null && "type" in cell && cell.type === "image";
          }),
      );
    if (hasImageThumbnail) {
      throw new Error(
        "This preview only retains thumbnails. Run the recipe again to download the original images.",
      );
    }
  }

  await triggerClientJsonlDownload(execution.dataset, filenameStem);
  return hasCompleteLocalDataset(execution) ? "saved" : "partial";
}
