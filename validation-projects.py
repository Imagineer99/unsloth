"""Pinned base/head project lifecycle. Each invocation is a fresh backend process."""
import argparse
import json
import os
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("phase", choices=("seed", "select", "restart", "offline", "control"))
parser.add_argument("source", type=Path)
parser.add_argument("home", type=Path)
args = parser.parse_args()
home = args.home.resolve()
home.mkdir(parents=True, exist_ok=True)
chosen = home / "Chosen projects"
os.environ["UNSLOTH_STUDIO_HOME"] = str(home / "db")
os.environ["UNSLOTH_STUDIO_DOCUMENTS_HOME"] = str(home / "Documents")
os.environ.pop("UNSLOTH_STUDIO_PROJECTS_HOME", None)
sys.path.insert(0, str(args.source.resolve() / "studio/backend"))
from fastapi import FastAPI
from fastapi.testclient import TestClient
from auth.authentication import get_current_subject, authenticated_via_api_key
from routes import chat_history, settings
from storage import studio_db

app = FastAPI()
app.dependency_overrides[get_current_subject] = lambda: "owner"
app.dependency_overrides[authenticated_via_api_key] = lambda: False
app.include_router(settings.router, prefix="/api/settings")
app.include_router(chat_history.router, prefix="/api/chat")
client = TestClient(app, raise_server_exceptions=False)
setting = "/api/settings/projects-home"
def create(name):
    row = studio_db.upsert_chat_project(dict(id=name, name=name, createdAt=1, updatedAt=1))
    return Path(row["rootPath"])

def reopen(name):
    response = client.get("/api/chat/projects/" + name)
    assert response.status_code == 200, response.text
    return Path(response.json()["rootPath"])

def select(path):
    response = client.put(setting, json={"projects_home": path})
    assert response.status_code == 200, response.text
    return response.json()

if args.phase == "seed":
    assert client.get(setting).status_code == 404
    a = create("A")
    (a / "sentinel").write_text("original A")
    (home / "a-path").write_text(str(a), encoding="utf-8")
elif args.phase == "select":
    chosen.mkdir()
    assert select(str(chosen))["source"] == "studio"
elif args.phase == "restart":
    assert client.get(setting).json()["projects_home"] == str(chosen)
    a = reopen("A")
    assert str(a) == (home / "a-path").read_text(encoding="utf-8")
    assert (a / "sentinel").read_text() == "original A"
    b = create("B")
    assert b.parent == chosen
    (b / "sentinel").write_text("original B")
    assert select(None)["source"] == "default"
    assert create("C").parent == a.parent
    assert (reopen("B") / "sentinel").read_text() == "original B"
else:
    if args.phase == "control":
        chosen.mkdir()
        os.environ["UNSLOTH_STUDIO_PROJECTS_HOME"] = str(chosen)
        b = create("B")
        assert b.parent == chosen
        (b / "sentinel").write_text("original B")
        del os.environ["UNSLOTH_STUDIO_PROJECTS_HOME"]
    offline = home / "offline"
    chosen.rename(offline)
    response = client.get("/api/chat/projects/B")
    recreated = chosen.exists()
    if recreated:
        chosen.rename(home / "replacement")
    offline.rename(chosen)
    assert (reopen("B") / "sentinel").read_text() == "original B"
    print(json.dumps(dict(phase=args.phase, status=response.status_code, recreated=recreated, original_restored=True)), flush=True)
    if args.phase == "offline":
        assert response.is_error and not recreated, "Unavailable storage silently reopened or recreated"
print(args.phase, "finished")
