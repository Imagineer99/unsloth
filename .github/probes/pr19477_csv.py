"""Exercise local CSV evaluation with deterministic generations on a pinned checkout."""
import csv
import json
import os
import platform
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

side, expected_sha, source_arg, output_arg = sys.argv[1:]
source = Path(source_arg).resolve()
output = Path(output_arg).resolve()
output.mkdir(parents=True, exist_ok=True)
sha = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
assert sha == expected_sha, (sha, expected_sha)
os.environ.update(HF_HOME=str(output / "hf"), LM_HARNESS_CACHE_PATH=str(output / "lm-cache"),
                  HF_HUB_OFFLINE="1", HF_DATASETS_OFFLINE="1", HF_DATASETS_DISABLE_PROGRESS_BARS="1")
sys.path.insert(0, str(source))
from lm_eval import evaluator
from lm_eval.api.model import LM
from lm_eval.tasks import TaskManager
from unsloth_cli.commands.eval import resolve_tasks

class Fixed(LM):
    def __init__(self, predictions):
        super().__init__()
        self.predictions = predictions
        self.prompts = []

    def generate_until(self, requests):
        self.prompts.extend(request.args[0] for request in requests)
        return [self.predictions[request.doc_id] for request in requests]

    def loglikelihood(self, requests):
        raise AssertionError("Unexpected likelihood request")

    def loglikelihood_rolling(self, requests):
        raise AssertionError("Unexpected rolling likelihood request")

def load_task(name, rows):
    data = output / f"{name}.csv"
    with data.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["question", "answer", "id"])
        writer.writerows(rows)
    names, includes = resolve_tasks(str(data), "question", "answer", output / name)
    return TaskManager(include_defaults=False, include_path=includes).load_task_or_group(names)

print("ENV", side, sha, platform.platform(), sys.version.split()[0],
      {p: version(p) for p in ("lm-eval", "datasets", "pandas", "pyarrow", "torch")}, flush=True)
tasks = load_task("codes", [("001", "007", 1), ("010", "042", 2)])
task = tasks["codes"]
refs = [task.doc_to_target(doc) for doc in task.test_docs()]
assert refs == (["7", "42"] if side == "base" else ["007", "042"]), refs
for predictions, expected_score in ((["007", "042"], float(side == "head")),
                                    (["7", "42"], float(side == "base"))):
    model = Fixed(predictions)
    result = evaluator.evaluate(lm=model, task_dict=tasks, bootstrap_iters=0)
    score = result["results"]["codes"]["exact_match,strip"]
    print("SCORE", side, json.dumps(dict(prompts=model.prompts, refs=refs,
                                        predictions=predictions, score=score)), flush=True)
    assert model.prompts == (["1", "10"] if side == "base" else ["001", "010"]), model.prompts
    assert score == expected_score, score

rows = (("Q", "A", i if i < 10000 else "NA") for i in range(10001))
print("METADATA", side, "loading 10001 rows", flush=True)
docs = load_task("metadata", rows)["metadata"].test_docs()
assert len(docs) == 10001 and docs[-1]["id"] is None, (len(docs), docs[-1])
print("METADATA", side, "PASS rows=10001 last_id=null", flush=True)
