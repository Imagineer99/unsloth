"""Compare read_skill_resource on the two pinned revisions using identical NTFS fixtures."""
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

revision, expected_sha, side = sys.argv[1:]
checkout = Path(revision).resolve()
actual_sha = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
assert actual_sha == expected_sha, (actual_sha, expected_sha)
print(f"{side} revision {actual_sha}", flush=True)
sys.path.insert(0, str(checkout / "studio/backend"))

with tempfile.TemporaryDirectory() as tmp:
    home = Path(tmp)
    os.environ["UNSLOTH_STUDIO_HOME"] = str(home / "app")
    from core.inference import skills

    assert os.name == "nt" and not skills._SCANDIR_FD
    root = home / ".agents/skills/reader"
    (root / "scripts").mkdir(parents=True)
    (root / "cache").mkdir()
    (root / "SKILL.md").write_text("---\nname: reader\ndescription: Test skill.\n---\nRead inputs.", encoding="utf-8")
    (root / "scripts/lint.py").write_text("VALID SKILL FILE", encoding="utf-8")
    (root / "desktop.ini").touch()
    (root / "cache/notes.txt").touch()
    for hidden in (root / "desktop.ini", root / "cache"):
        subprocess.run(["attrib", "+H", str(hidden)], check=True)
        assert hidden.stat().st_file_attributes & stat.FILE_ATTRIBUTE_HIDDEN

    result = skills.read_skill_resource("reader", "scripts/lint.py", home=home)
    assert result.endswith("VALID SKILL FILE"), result
    print(result)
    try:
        skills.read_skill_resource("reader", "data.jsonl", home=home)
    except skills.SkillError as exc:
        error = str(exc)
    else:
        raise AssertionError("Missing skill resource unexpectedly read successfully")
    print(error)
    guidance, separator, listing = error.partition(" Files in this skill: ")
    assert separator and guidance.endswith("Chat attachments are not skill resources."), error
    names = set(listing.removesuffix(".").split(", "))
    expected = {"SKILL.md", "scripts/lint.py"}
    if side == "original":
        expected.update({"desktop.ini", "cache/notes.txt"})
    else:
        assert side == "corrected"
    assert names == expected, (names, expected)
    print(f"PASS {side}: exact listing, attachment guidance, and valid resource read")
