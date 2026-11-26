const esbuild = require("esbuild");
const fs = require("node:fs");
const path = require("node:path");
const { execFileSync } = require("node:child_process");

async function main() {
  const source = path.resolve(process.env.SOURCE || "source");
  const frontend = path.join(source, "studio/frontend");
  const hook = "studio/frontend/src/features/audio/hooks/use-stem-transport.ts";
  fs.mkdirSync("audio-site", { recursive: true });
  for (const rev of ["base", "head"]) {
    const contents = execFileSync("git", ["-C", source, "show", process.env[rev.toUpperCase()] + ":" + hook], { encoding: "utf8" });
    await esbuild.build({
      stdin: {
        contents: fs.readFileSync(path.join(__dirname, "mixer.jsx"), "utf8"),
        resolveDir: frontend, loader: "jsx",
      },
      alias: { "@": path.join(frontend, "src") },
      plugins: [{
        name: "pinned-transport",
        setup(build) {
          build.onResolve({ filter: /^\.\.\/hooks\/use-stem-transport$/ }, () => ({ path: hook, namespace: "pinned" }));
          build.onLoad({ filter: /.*/, namespace: "pinned" }, () => ({ contents, loader: "ts", resolveDir: frontend }));
        },
      }],
      bundle: true, minify: true, jsx: "automatic", target: "es2022", outfile: "audio-site/" + rev + ".js",
    });
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
