#!/usr/bin/env python3
"""Pin the diagnostic's four states: download each codeload archive, check the commit it
carries, hash the files the script verifies FROM THAT ARCHIVE, and write diag-manifest.json
plus the copy embedded in unsloth-win-diag.ps1.

  python make_manifest.py base=unslothai/unsloth@SHA stack=unslothai/unsloth@SHA \
      presence=OWNER/REPO@SHA combined=OWNER/REPO@SHA
"""

import datetime
import hashlib
import io
import json
import re
import sys
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILES = ["install.ps1", "studio/setup.ps1", "scripts/uninstall.ps1", "pyproject.toml"]
ORDER = ["base", "stack", "presence", "combined"]


def pin(repo: str, sha: str) -> dict:
    url = f"https://codeload.github.com/{repo}/zip/{sha}"
    data = urllib.request.urlopen(url, timeout = 300).read()
    zf = zipfile.ZipFile(io.BytesIO(data))
    comment = zf.comment.decode("ascii", "replace")
    if comment != sha:
        raise SystemExit(f"{url}: archive comment {comment!r} is not {sha}")
    top = zf.namelist()[0].split("/", 1)[0]
    files = {}
    for rel in FILES:
        files[rel] = hashlib.sha256(zf.read(f"{top}/{rel}")).hexdigest()
    print(f"{repo}@{sha[:12]}: {len(data) / 1e6:.1f} MB, {len(zf.namelist())} entries")
    return {"repo": repo, "sha": sha, "zip_url": url, "files": files}


def main() -> None:
    wanted = dict(a.split("=", 1) for a in sys.argv[1:])
    if sorted(wanted) != sorted(ORDER):
        raise SystemExit(f"need exactly {', '.join(ORDER)}")
    states = {}
    for name in ORDER:
        repo, sha = wanted[name].split("@", 1)
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise SystemExit(f"{name}: give the full 40-character sha")
        states[name] = pin(repo, sha)
    manifest = {"schema": 1, "created": datetime.date.today().isoformat(), "states": states}
    text = json.dumps(manifest, indent = 2)
    (HERE / "diag-manifest.json").write_text(text + "\n")
    ps1 = HERE / "unsloth-win-diag.ps1"
    src = ps1.read_text(encoding = "utf-8")
    new, n = re.subn(
        r"(\$EmbeddedManifest = @'\r?\n).*?(\r?\n'@)",
        lambda m: m.group(1) + text + m.group(2),
        src,
        count = 1,
        flags = re.S,
    )
    if n != 1:
        raise SystemExit("embedded manifest block not found")
    ps1.write_text(new, encoding = "utf-8")
    print("wrote diag-manifest.json and the embedded copy")


if __name__ == "__main__":
    main()
