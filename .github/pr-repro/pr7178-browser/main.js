import DOMPurify from "dompurify";
import * as yaml from "js-yaml";
import mermaid from "mermaid";

const tests = [];
const check = (name, pass, detail = "") => tests.push({ name, pass: Boolean(pass), detail });

try {
  const clean = DOMPurify.sanitize('<svg><script>bad()</script></svg><img src="x" onerror="bad()">');
  check("default-xss-stripping", !clean.includes("script") && !clean.includes("onerror"), clean);

  DOMPurify.addHook("uponSanitizeElement", (_node, data) => {
    data.allowedTags.script = true;
  });
  DOMPurify.sanitize("<svg><script>trusted()</script></svg>");
  DOMPurify.removeAllHooks();
  DOMPurify.clearConfig();
  const afterHook = DOMPurify.sanitize("<svg><script>attacker()</script></svg>");
  check("hook-allowlist-does-not-persist", !afterHook.includes("script"), afterHook);

  DOMPurify.setConfig({ ALLOWED_TAGS: ["img"], ALLOWED_ATTR: ["src"] });
  DOMPurify.addHook("uponSanitizeAttribute", (node, data) => {
    if (node.getAttribute?.("data-trusted") === "1") data.allowedAttributes.onerror = true;
  });
  DOMPurify.sanitize('<img data-trusted="1" src="x" onerror="trusted()">');
  DOMPurify.removeAllHooks();
  const afterPersistentConfig = DOMPurify.sanitize('<img src="x" onerror="attacker()">');
  check("set-config-hook-does-not-persist", !afterPersistentConfig.includes("onerror"), afterPersistentConfig);
  DOMPurify.clearConfig();

  const inPlaceHost = document.createElement("div");
  const disguisedScript = document.createElement("script");
  disguisedScript.textContent = "window.__unexpectedScriptExecution = true";
  Object.defineProperty(disguisedScript, "nodeName", { value: "DIV", configurable: true });
  inPlaceHost.appendChild(disguisedScript);
  DOMPurify.sanitize(inPlaceHost, { IN_PLACE: true });
  check("in-place-node-name-clobber", !inPlaceHost.querySelector("script"), inPlaceHost.outerHTML);

  const foreignFrame = document.createElement("iframe");
  foreignFrame.src = "about:blank";
  document.body.appendChild(foreignFrame);
  const foreignHost = foreignFrame.contentDocument.createElement("div");
  foreignHost.innerHTML = '<img src="x" onerror="attacker()"><template><script>bad()</script></template>';
  DOMPurify.sanitize(foreignHost, { IN_PLACE: true });
  check(
    "cross-realm-in-place",
    !foreignHost.querySelector("script") && !foreignHost.querySelector("[onerror]"),
    foreignHost.outerHTML,
  );
  foreignFrame.remove();
} catch (error) {
  check("dompurify-harness", false, String(error?.stack || error));
}

try {
  const normal = yaml.load("model: unsloth/test\nmax_seq_length: 4096\nflags:\n  - fast\n");
  check("yaml-normal-config", normal.model === "unsloth/test" && normal.flags[0] === "fast");
  const dumped = yaml.dump(normal, { lineWidth: -1, noRefs: true });
  check("yaml-round-trip", yaml.load(dumped).max_seq_length === 4096, dumped);

  const size = 6000;
  const keys = Array.from({ length: size }, (_, index) => `k${index}: 0`).join(", ");
  const aliases = Array.from({ length: size }, () => "*a").join(", ");
  const hostileYaml = `a: &a {${keys}}\nb: {<<: [${aliases}]}\n`;
  const started = performance.now();
  try {
    const parsed = yaml.load(hostileYaml);
    const elapsedMs = performance.now() - started;
    check(
      "yaml-duplicate-merge-dos-fixed",
      Object.keys(parsed.b).length === size && elapsedMs < 1000,
      `parsed in ${elapsedMs.toFixed(1)}ms`,
    );
  } catch (error) {
    const elapsedMs = performance.now() - started;
    check(
      "yaml-duplicate-merge-dos-fixed",
      String(error).includes("maxTotalMergeKeys") && elapsedMs < 1000,
      `rejected in ${elapsedMs.toFixed(1)}ms: ${String(error).slice(0, 160)}`,
    );
  }
} catch (error) {
  check("js-yaml-harness", false, String(error?.stack || error));
}

try {
  mermaid.initialize({ startOnLoad: false, securityLevel: "strict" });
  const graph = 'flowchart LR\nA["<img src=x onerror=alert(1)>"] --> B[Safe]\nclick A "javascript:alert(1)"';
  const rendered = await mermaid.render("securityGraph", graph);
  const svg = rendered.svg.toLowerCase();
  check(
    "mermaid-hostile-label-sanitized",
    !svg.includes("onerror") && !svg.includes("javascript:") && !svg.includes("<script"),
    svg.slice(0, 500),
  );
} catch (error) {
  check("mermaid-render", false, String(error?.stack || error));
}

window.__securityResults = {
  versions: { dompurify: DOMPurify.version },
  userAgent: navigator.userAgent,
  passed: tests.filter((test) => test.pass).length,
  failed: tests.filter((test) => !test.pass).length,
  tests,
};
document.querySelector("#results").textContent = JSON.stringify(window.__securityResults, null, 2);
