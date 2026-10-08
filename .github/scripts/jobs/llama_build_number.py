"""Base vs head of unslothai/unsloth#13036 on the real runner OS: does a --depth 1 llama.cpp
checkout of a bNNNN tag stamp build 1 without -DLLAMA_BUILD_NUMBER (base) and the tag's number
with it (head)? Clones the newest upstream bNNNN tag shallowly the way setup.sh / setup.ps1 do,
runs cmake configure twice (no compile), and reads the generated common/build-info.cpp.
Exit 0 only when base == 1 and head == N. Extra args (e.g. --tiny) are ignored."""

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = "https://github.com/ggml-org/llama.cpp"


def run(cmd, **kw):
    print("+", " ".join(map(str, cmd)), flush = True)
    return subprocess.run(cmd, check = True, text = True, capture_output = True, **kw)


def newest_build_tag() -> str:
    out = run(["git", "ls-remote", "--tags", "--refs", REPO]).stdout
    builds = [int(m.group(1)) for m in re.finditer(r"refs/tags/b(\d+)$", out, re.M)]
    return f"b{max(builds)}"


def stamped(src: Path, build: Path, extra: list) -> int:
    cmd = [
        "cmake",
        "-S",
        str(src),
        "-B",
        str(build),
        "-DCMAKE_BUILD_TYPE=Release",
        "-DLLAMA_BUILD_TESTS=OFF",
        "-DLLAMA_BUILD_EXAMPLES=OFF",
        "-DLLAMA_BUILD_SERVER=ON",
        "-DGGML_NATIVE=OFF",
        "-DLLAMA_CURL=OFF",
        "-DLLAMA_OPENSSL=OFF",
        *extra,
    ]
    try:
        run(cmd)
    except subprocess.CalledProcessError as exc:
        print(exc.stdout[-3000:], exc.stderr[-3000:])
        raise
    text = (build / "common" / "build-info.cpp").read_text(encoding = "utf-8")
    return int(re.search(r"LLAMA_BUILD_NUMBER\s*=\s*(\d+)", text).group(1))


def main() -> int:
    tag = newest_build_tag()
    n = int(tag[1:])
    work = Path(tempfile.mkdtemp(prefix = "bn_"))
    try:
        src = work / "llama.cpp"
        run(["git", "clone", "-q", "--depth", "1", "--branch", tag, REPO + ".git", str(src)])
        count = run(["git", "-C", str(src), "rev-list", "--count", "HEAD"]).stdout.strip()
        base = stamped(src, work / "build_base", [])
        head = stamped(src, work / "build_head", [f"-DLLAMA_BUILD_NUMBER={n}"])
    finally:
        shutil.rmtree(work, ignore_errors = True)
    ok = base == 1 and head == n
    print(
        json.dumps(
            {
                "os": sys.platform,
                "tag": tag,
                "rev_list_count": count,
                "base_build_number": base,
                "head_build_number": head,
                "pass": ok,
            }
        )
    )
    print(f"BUILD_NUMBER_VERDICT {'PASS' if ok else 'FAIL'} base={base} head={head} tag={tag}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
