"""Resolve the one known PR #9595/current-main merge overlap deterministically."""

from __future__ import annotations

import argparse
from pathlib import Path


CONFLICT = "\n".join(
    (
        "@lru_cache(maxsize = 128)",
        "<" * 7 + " HEAD",
        "def _recommended_sampling(model_id: str) -> Dict[str, Any]:",
        '    \"\"\"Per-model recommended sampling, resolved through the SAME path the Unsloth Chat UI uses.',
        "=" * 7,
        "def _recommended_sampling(model_id: str, thinking_mode: Optional[bool] = None) -> Dict[str, Any]:",
        '    \"\"\"Per-model recommended sampling, resolved through the SAME path the Studio Chat UI uses.',
        ">" * 7 + " 870cf61305bec2d07f08e6aabb06145784f18f4b",
        "",
    )
)

RESOLUTION = """@lru_cache(maxsize = 128)
def _recommended_sampling(model_id: str, thinking_mode: Optional[bool] = None) -> Dict[str, Any]:
    \"\"\"Per-model recommended sampling, resolved through the SAME path the Unsloth Chat UI uses.
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("integration_root")
    args = parser.parse_args()

    target = (
        Path(args.integration_root)
        / "studio/backend/utils/inference/inference_config.py"
    )
    source = target.read_text(encoding="utf-8")
    count = source.count(CONFLICT)
    if count != 1:
        raise SystemExit(f"expected exactly one known conflict block, found {count}")
    target.write_text(source.replace(CONFLICT, RESOLUTION), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
