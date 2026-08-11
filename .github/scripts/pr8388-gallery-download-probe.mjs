import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

const repo = process.cwd();
const imagePath = path.join(
  repo,
  "studio/frontend/src/features/images/images-page.tsx",
);
const videoPath = path.join(
  repo,
  "studio/frontend/src/features/video/video-page.tsx",
);

const [imageSource, videoSource] = await Promise.all([
  readFile(imagePath, "utf8"),
  readFile(videoPath, "utf8"),
]);

function extractFunction(source, name, optional = false) {
  const token = `function ${name}(`;
  let start = source.indexOf(token);
  if (start < 0) {
    if (optional) return "";
    throw new Error(`Could not find ${name}`);
  }
  if (source.slice(start - 6, start) === "async ") start -= 6;
  const open = source.indexOf("{", start);
  let depth = 0;
  for (let index = open; index < source.length; index += 1) {
    if (source[index] === "{") depth += 1;
    if (source[index] === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, index + 1);
    }
  }
  throw new Error(`Could not find the end of ${name}`);
}

function extractHandleDownload(source) {
  const start = source.indexOf("const handleDownload = useCallback(");
  const end = source.indexOf("\n\n  const handleDelete", start);
  if (start < 0 || end < 0) throw new Error("Could not find handleDownload");
  return source.slice(start, end);
}

async function loadExtractedModule(label, preamble, pieces, exportsList, mocks) {
  const directory = await mkdtemp(path.join(tmpdir(), `pr8388-${label}-`));
  const file = path.join(directory, `${label}.ts`);
  await writeFile(
    file,
    `${preamble}\n${pieces.filter(Boolean).join("\n\n")}\nexport { ${exportsList.join(", ")} };\n`,
    "utf8",
  );
  globalThis.__pr8388Mocks = mocks;
  return import(`${pathToFileURL(file).href}?run=${Date.now()}-${Math.random()}`);
}

function makeDom(state, config) {
  class MockImage {
    naturalWidth = 2;
    naturalHeight = 2;
    decoding = "auto";
    src = "";

    async decode() {}
  }

  const document = {
    createElement(tag) {
      if (tag === "a") {
        return {
          href: "",
          download: "",
          rel: "",
          click() {
            state.anchors.push({ href: this.href, filename: this.download });
          },
          remove() {},
        };
      }
      if (tag === "canvas") {
        return {
          width: 0,
          height: 0,
          getContext() {
            return {
              fillStyle: "",
              fillRect() {},
              drawImage() {},
            };
          },
          toBlob(callback, requestedType) {
            const returnedType =
              requestedType === "image/webp" && config.webpFallsBackToPng
                ? "image/png"
                : requestedType;
            callback(new Blob([new Uint8Array([1, 2, 3])], { type: returnedType }));
          },
        };
      }
      throw new Error(`Unexpected element: ${tag}`);
    },
    body: {
      appendChild() {},
    },
  };

  const URL = {
    createObjectURL(blob) {
      const url = `blob:probe-${state.objectUrls.length + 1}`;
      state.objectUrls.push({ url, type: blob.type });
      return url;
    },
    revokeObjectURL(url) {
      state.revoked.push(url);
    },
  };

  return { document, Image: MockImage, URL };
}

function freshState() {
  return {
    anchors: [],
    browserDownloads: [],
    nativeDownloads: [],
    originalFetches: [],
    exportFetches: [],
    streamingDownloads: [],
    objectUrls: [],
    revoked: [],
    successToasts: [],
    errorToasts: [],
    loadingToasts: [],
    dismissedToasts: [],
  };
}

function makeImageMocks(isTauri) {
  const state = freshState();
  const config = {
    webpFallsBackToPng: false,
    nativeError: null,
  };
  const dom = makeDom(state, config);
  return {
    state,
    config,
    mocks: {
      ...dom,
      isTauri,
      async fetchGalleryBlob(url) {
        state.originalFetches.push(url);
        return new Blob([new Uint8Array([137, 80, 78, 71])], { type: "image/png" });
      },
      async downloadFile(content, filename, mimeType) {
        if (config.nativeError) throw config.nativeError;
        state.nativeDownloads.push({ filename, mimeType, contentType: content.type });
      },
      async downloadUrl(url, filename) {
        state.browserDownloads.push({ url, filename });
      },
      isDownloadCancelled(error) {
        return error?.cancelled === true;
      },
      toast: {
        success(...args) {
          state.successToasts.push(args);
        },
        error(...args) {
          state.errorToasts.push(args);
        },
      },
      setTimeout() {
        return 0;
      },
    },
  };
}

function makeVideoMocks(isTauri) {
  const state = freshState();
  const config = {
    webpFallsBackToPng: false,
    nativeError: null,
    streamingGate: null,
  };
  const dom = makeDom(state, config);
  return {
    state,
    config,
    mocks: {
      ...dom,
      isTauri,
      useCallback(callback) {
        return callback;
      },
      async fetchGalleryVideoExport(id, format) {
        state.exportFetches.push({ id, format });
        return new Blob([new Uint8Array([4, 5, 6])], { type: `video/${format}` });
      },
      async downloadFile(content, filename, mimeType) {
        if (config.nativeError) throw config.nativeError;
        state.nativeDownloads.push({ filename, mimeType, contentType: content.type });
      },
      async downloadUrlStreaming(url, filename) {
        state.streamingDownloads.push({ url, filename });
        if (config.streamingGate) await config.streamingGate;
        if (config.nativeError) throw config.nativeError;
      },
      isDownloadCancelled(error) {
        return error?.cancelled === true;
      },
      toast: {
        loading(...args) {
          state.loadingToasts.push(args);
          return `loading-${state.loadingToasts.length}`;
        },
        dismiss(...args) {
          state.dismissedToasts.push(args);
        },
        success(...args) {
          state.successToasts.push(args);
        },
        error(...args) {
          state.errorToasts.push(args);
        },
      },
      setTimeout() {
        return 0;
      },
    },
  };
}

const imagePieces = [
  extractFunction(imageSource, "exportFilename"),
  extractFunction(imageSource, "saveBlobUrl", true),
  extractFunction(imageSource, "reencodeImage", true),
  extractFunction(imageSource, "downloadImage"),
];
const imagePreamble = `
const {
  document, Image, URL, isTauri, fetchGalleryBlob, downloadFile, downloadUrl,
  isDownloadCancelled, toast, setTimeout,
} = globalThis.__pr8388Mocks;
`;

const desktopImage = makeImageMocks(true);
const imageModule = await loadExtractedModule(
  "images-desktop",
  imagePreamble,
  imagePieces,
  ["downloadImage"],
  desktopImage.mocks,
);

const galleryImage = {
  id: "image-1",
  url: "/api/inference/images/gallery/image-1/file",
  created_at: 1_788_000_000,
  seed: 4242,
  batch_index: 0,
};

let passed = 0;
let failed = 0;

async function probe(name, callback) {
  try {
    await callback();
    passed += 1;
    console.log(`PASS ${name}`);
  } catch (error) {
    failed += 1;
    console.log(`FAIL ${name}: ${error.message}`);
  }
}

function clearState(state) {
  for (const value of Object.values(state)) {
    if (Array.isArray(value)) value.length = 0;
  }
}

await probe("desktop PNG re-fetches the authenticated original and saves natively", async () => {
  clearState(desktopImage.state);
  desktopImage.config.webpFallsBackToPng = false;
  await imageModule.downloadImage("blob:cached-image", galleryImage, "png");
  assert.deepEqual(desktopImage.state.originalFetches, [galleryImage.url]);
  assert.equal(desktopImage.state.nativeDownloads.length, 1);
  assert.match(desktopImage.state.nativeDownloads[0].filename, /\.png$/);
  assert.equal(desktopImage.state.anchors.length, 0);
  assert.equal(desktopImage.state.successToasts[0]?.[0], "Image saved");
});

await probe("desktop JPEG conversion uses the shared native save helper", async () => {
  clearState(desktopImage.state);
  desktopImage.config.webpFallsBackToPng = false;
  await imageModule.downloadImage("blob:cached-image", galleryImage, "jpeg");
  assert.equal(desktopImage.state.originalFetches.length, 0);
  assert.equal(desktopImage.state.nativeDownloads.length, 1);
  assert.match(desktopImage.state.nativeDownloads[0].filename, /\.jpg$/);
  assert.equal(desktopImage.state.nativeDownloads[0].contentType, "image/jpeg");
  assert.equal(desktopImage.state.anchors.length, 0);
});

await probe("WebKit-style unsupported WebP encoding falls back to a PNG filename", async () => {
  clearState(desktopImage.state);
  desktopImage.config.webpFallsBackToPng = true;
  await imageModule.downloadImage("blob:cached-image", galleryImage, "webp");
  assert.equal(desktopImage.state.nativeDownloads.length, 1);
  assert.match(desktopImage.state.nativeDownloads[0].filename, /\.png$/);
  assert.equal(desktopImage.state.nativeDownloads[0].contentType, "image/png");
  assert.deepEqual(desktopImage.state.originalFetches, [galleryImage.url]);
  assert.equal(desktopImage.state.anchors.length, 0);
});

await probe("browser PNG download remains non-native", async () => {
  const browserImage = makeImageMocks(false);
  const browserModule = await loadExtractedModule(
    "images-browser",
    imagePreamble,
    imagePieces,
    ["downloadImage"],
    browserImage.mocks,
  );
  await browserModule.downloadImage("blob:cached-image", galleryImage, "png");
  assert.equal(browserImage.state.nativeDownloads.length, 0);
  assert.equal(
    browserImage.state.browserDownloads.length + browserImage.state.anchors.length,
    1,
  );
  assert.equal(browserImage.state.successToasts.length, 0);
});

const videoPieces = [
  extractFunction(videoSource, "exportFilename"),
  extractFunction(videoSource, "saveLink", true),
  extractFunction(videoSource, "downloadVideo"),
  extractHandleDownload(videoSource),
];
const videoPreamble = `
const {
  document, URL, isTauri, useCallback, fetchGalleryVideoExport, downloadFile,
  downloadUrlStreaming, isDownloadCancelled, toast, setTimeout,
} = globalThis.__pr8388Mocks;
`;
const desktopVideo = makeVideoMocks(true);
const videoModule = await loadExtractedModule(
  "videos-desktop",
  videoPreamble,
  videoPieces,
  ["downloadVideo", "handleDownload"],
  desktopVideo.mocks,
);
const galleryVideo = {
  id: "video-1",
  created_at: "2026-08-11T12:34:56Z",
  seed: 8181,
};

await probe("MP4 handler awaits native completion before confirming the save", async () => {
  clearState(desktopVideo.state);
  let releaseSave;
  desktopVideo.config.streamingGate = new Promise((resolve) => {
    releaseSave = resolve;
  });
  let settled = false;
  const pending = videoModule
    .handleDownload("http://127.0.0.1:8000/video.mp4", galleryVideo, "mp4")
    .then(() => {
      settled = true;
    });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(settled, false, "handler returned before the native save completed");
  assert.equal(desktopVideo.state.successToasts.length, 0);
  releaseSave();
  await pending;
  assert.equal(desktopVideo.state.streamingDownloads.length, 1);
  assert.equal(desktopVideo.state.successToasts[0]?.[0], "Video saved");
  desktopVideo.config.streamingGate = null;
});

await probe("WebM export saves through the native helper and confirms completion", async () => {
  clearState(desktopVideo.state);
  await videoModule.handleDownload("blob:cached-video", galleryVideo, "webm");
  assert.deepEqual(desktopVideo.state.exportFetches, [{ id: "video-1", format: "webm" }]);
  assert.equal(desktopVideo.state.nativeDownloads.length, 1);
  assert.match(desktopVideo.state.nativeDownloads[0].filename, /\.webm$/);
  assert.equal(desktopVideo.state.anchors.length, 0);
  assert.equal(desktopVideo.state.successToasts[0]?.[0], "Video saved");
});

await probe("GIF native-save failures are surfaced with the real error", async () => {
  clearState(desktopVideo.state);
  desktopVideo.config.nativeError = new Error("disk full");
  await videoModule.handleDownload("blob:cached-video", galleryVideo, "gif");
  assert.equal(desktopVideo.state.errorToasts[0]?.[0], "Could not save video");
  assert.equal(desktopVideo.state.errorToasts[0]?.[1]?.description, "disk full");
  assert.equal(desktopVideo.state.successToasts.length, 0);
  desktopVideo.config.nativeError = null;
});

await probe("native save-dialog cancellation is silent", async () => {
  clearState(desktopVideo.state);
  desktopVideo.config.nativeError = { cancelled: true };
  await videoModule.handleDownload("blob:cached-video", galleryVideo, "webm");
  assert.equal(desktopVideo.state.errorToasts.length, 0);
  assert.equal(desktopVideo.state.successToasts.length, 0);
  desktopVideo.config.nativeError = null;
});

console.log(`SUMMARY ${passed} passed, ${failed} failed`);
if (failed > 0) process.exitCode = 1;
