import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const moduleUrl = pathToFileURL(
  `${process.cwd()}/studio/frontend/src/lib/gguf-fit.ts`,
).href;
const { classifyGgufFit } = await import(moduleUrl);

const GiB = 1024 ** 3;
const oversizedBytes = 90 * GiB;
const resources = { gpuGb: 16, systemRamGb: 64 };

const actual = {
  smallFits: classifyGgufFit(4 * GiB, {
    ...resources,
    diskFreeGb: 200,
  }),
  oversizedEnoughDisk: classifyGgufFit(oversizedBytes, {
    ...resources,
    diskFreeGb: 200,
    downloadBytes: oversizedBytes,
  }),
  oversizedNoSpace: classifyGgufFit(oversizedBytes, {
    ...resources,
    diskFreeGb: 50,
    downloadBytes: oversizedBytes,
  }),
  oversizedAlreadyOnDisk: classifyGgufFit(oversizedBytes, {
    ...resources,
    diskFreeGb: 1,
    onDisk: true,
  }),
  oversizedUnreadDiskProbe: classifyGgufFit(oversizedBytes, {
    ...resources,
    diskFreeGb: 0,
  }),
};

console.log(`PR9920_ACTUAL ${JSON.stringify(actual)}`);

assert.equal(actual.smallFits, "fits", "control: a small quant must still fit");
assert.deepEqual(
  actual,
  {
    smallFits: "fits",
    oversizedEnoughDisk: "disk",
    oversizedNoSpace: "nospace",
    oversizedAlreadyOnDisk: "disk",
    oversizedUnreadDiskProbe: "disk",
  },
  "PR #9920 expected the oversized GGUF to become a disk tier, with the disk-space floor applied only when measurable and still needing download",
);

console.log("PASS PR9920 disk-tier classifier and disk-floor semantics");
