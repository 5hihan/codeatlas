"""The index-root guard: only project folders get indexed, never $HOME, user folders, drive roots or system dirs."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from codeatlas import roots


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    h = tmp_path / "home" / "me"
    for name in ("Desktop", "Documents", "Downloads", "AppData/Local", "OneDrive/Documents", "Projects"):
        (h / name).mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    monkeypatch.delenv("CODEATLAS_ALLOW_ANY_ROOT", raising=False)
    return h


def test_home_and_user_folders_are_refused(home: Path):
    assert "home directory" in roots.protected_reason(home)
    assert "contains your home" in roots.protected_reason(home.parent)
    for name in ("Desktop", "Documents", "Downloads", "AppData", "Projects"):
        assert "not a project" in roots.protected_reason(home / name), name
    assert "application-data" in roots.protected_reason(home / "AppData" / "Local")
    assert "not a project" in roots.protected_reason(home / "OneDrive")
    assert "cloud-sync" in roots.protected_reason(home / "OneDrive - Personal")
    assert "synced user folder" in roots.protected_reason(home / "OneDrive" / "Documents")


def test_projects_inside_user_folders_are_fine(home: Path):
    for p in (home / "Desktop" / "my-app", home / "Documents" / "work" / "svc", home / "Projects" / "x",
              home / "AppData" / "Local" / "Temp" / "pytest-1" / "repo", home / "OneDrive" / "Documents" / "app"):
        p.mkdir(parents=True, exist_ok=True)
        assert roots.protected_reason(p) is None, p


def test_roots_and_system_dirs_are_refused(home: Path, monkeypatch):
    anchor = Path(Path.cwd().anchor)
    assert "root" in roots.protected_reason(anchor)
    if os.name == "nt":
        monkeypatch.setenv("SystemDrive", "C:")
        monkeypatch.setenv("ProgramFiles", r"C:\Program Files")
        monkeypatch.setenv("SystemRoot", r"C:\Windows")
        assert "system directory" in roots.protected_reason(r"C:\Program Files\Git")
        assert "system directory" in roots.protected_reason(r"C:\Windows\System32")
        assert roots.protected_reason(r"C:\Users")  # container of all users (also an ancestor of the real home)
    else:
        assert "system directory" in roots.protected_reason("/usr/lib/python3")
        assert "many projects" in roots.protected_reason("/home")
        assert roots.protected_reason("/home/someone/app") is None or not Path("/home/someone/app").exists()


def test_override_env(home: Path, monkeypatch):
    monkeypatch.setenv("CODEATLAS_ALLOW_ANY_ROOT", "1")
    assert roots.protected_reason(home) is None


def test_index_refuses_unsafe_root(home: Path):
    from codeatlas.indexer import Index
    with pytest.raises(roots.UnsafeRootError, match="refusing to index"):
        Index(home / "Desktop")
    assert not (home / "Desktop" / ".codeatlas").exists()
    (home / "Desktop" / "proj").mkdir()
    ix = Index(home / "Desktop" / "proj")
    ix.close()


def test_cli_refuses_unsafe_root(home: Path, capsys):
    from codeatlas import cli
    with pytest.raises(SystemExit) as e:
        cli.main(["index", "--repo", str(home)])
    assert "refusing to index" in str(e.value)


def test_hook_ignores_stray_index_in_home(home: Path, monkeypatch):
    from codeatlas import hook
    (home / ".codeatlas").mkdir()
    (home / ".codeatlas" / "index.db").write_bytes(b"")
    proj = home / "Desktop" / "app"
    proj.mkdir(parents=True)
    assert hook.find_index(proj, stop=home.parent) is None
    (proj / ".codeatlas").mkdir()
    (proj / ".codeatlas" / "index.db").write_bytes(b"")
    assert hook.find_index(proj / "src", stop=home.parent) == proj.resolve() or hook.find_index(proj, stop=home.parent) == proj.resolve()
