"""Refuse to index locations that are not project folders.

An index root must be a project directory. The home directory, the well-known user folders inside it
(Desktop, Documents, Downloads, ...), drive / filesystem roots and system directories (Windows, Program
Files, /usr, /Applications, ...) are refused: indexing them walks the whole machine, produces a multi-GB
index and puts every file on the PC into an agent's context. Projects *inside* a user folder
(``~/Desktop/my-app``) are fine; anything inside a system directory is not.

``CODEATLAS_ALLOW_ANY_ROOT=1`` overrides the check for the rare deliberate case.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

# folders directly under the home directory (or under a cloud-sync root inside it) that are never projects
USER_FOLDERS = {"desktop", "documents", "downloads", "pictures", "music", "videos", "movies", "public",
                "appdata", "library", "onedrive", "dropbox", "icloud drive", "google drive", "my documents",
                "saved games", "contacts", "favorites", "links", "searches", ".config", ".local", ".cache",
                ".claude", ".codex", ".cursor", ".vscode", ".ssh", ".npm", ".cargo", ".rustup", ".m2", ".gradle",
                ".nvm", ".pyenv", ".conda", ".docker", "node_modules", "source", "repos", "projects", "code",
                "git", "github", "dev", "workspace", "workspaces", "src"}
SYNC_ROOTS = {"onedrive", "dropbox", "icloud drive", "google drive", "onedrive - personal"}
# system trees: the directory and everything below it is refused
POSIX_SYSTEM = ["/usr", "/etc", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/boot", "/dev", "/proc", "/sys",
                "/snap", "/System", "/Library", "/Applications"]
WINDOWS_ENV = ["SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData"]
# container roots: refused exactly, but projects inside them are fine (/home/me/app, /mnt/c/work/app, D:/Projects/app)
POSIX_CONTAINERS = ["/home", "/Users", "/root", "/var", "/opt", "/srv", "/tmp", "/private", "/mnt", "/media",
                    "/Volumes", "/mnt/c", "/mnt/d"]
WINDOWS_CONTAINERS = ["Users", "$Recycle.Bin", "System Volume Information", "Temp"]


class UnsafeRootError(ValueError):
    """Raised when asked to index a directory that is not a project folder."""


def _norm(p: Path) -> str:
    s = str(p).replace("\\", "/").rstrip("/")
    return s.lower() if os.name == "nt" else s


def _is_under(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent + "/")


def system_dirs() -> list[Path]:
    """Directories whose whole tree is refused."""
    if os.name != "nt":
        return [Path(p) for p in POSIX_SYSTEM]
    out = [Path(v) for k in WINDOWS_ENV if (v := os.environ.get(k))]
    drive = Path(os.environ.get("SystemDrive", "C:") + "/")
    out += [drive / "Windows", drive / "Program Files", drive / "Program Files (x86)", drive / "ProgramData"]
    return out


def container_dirs() -> list[Path]:
    """Directories refused exactly (they hold many projects); anything inside them is allowed."""
    if os.name != "nt":
        return [Path(p) for p in POSIX_CONTAINERS]
    drive = Path(os.environ.get("SystemDrive", "C:") + "/")
    return [drive / n for n in WINDOWS_CONTAINERS]


def protected_reason(path: Path | str) -> Optional[str]:
    """Why *path* must not be an index root, or None when it looks like a project folder."""
    if os.environ.get("CODEATLAS_ALLOW_ANY_ROOT", "").strip().lower() in ("1", "true", "yes"):
        return None
    p = Path(path).expanduser().resolve()
    n = _norm(p)
    if p == Path(p.anchor) or n in ("", "/") or (len(n) == 2 and n[1] == ":"):
        return f"{p} is a drive / filesystem root"
    home = Path.home().resolve()
    h = _norm(home)
    if n == h:
        return f"{p} is your home directory"
    if _is_under(h, n):
        return f"{p} contains your home directory"
    parent = _norm(p.parent)
    name = p.name.lower()
    if parent == h and name in USER_FOLDERS:
        return f"{p} is a user folder, not a project (index a project inside it instead)"
    if parent == h and name.startswith("onedrive"):
        return f"{p} is a cloud-sync root, not a project (index a project inside it instead)"
    # ~/OneDrive/Documents, ~/Dropbox/Desktop ...
    gp = _norm(p.parent.parent) if p.parent != p.parent.parent else ""
    if gp == h and (p.parent.name.lower() in SYNC_ROOTS or p.parent.name.lower().startswith("onedrive")) \
            and name in USER_FOLDERS:
        return f"{p} is a synced user folder, not a project (index a project inside it instead)"
    # ~/AppData/Local, ~/AppData/Roaming, ~/Library/Application Support ...
    if gp == h and p.parent.name.lower() in ("appdata", "library"):
        return f"{p} is an application-data folder, not a project"
    for d in system_dirs():
        dn = _norm(d)
        if dn and _is_under(n, dn):
            return f"{p} is inside the system directory {d}"
    for d in container_dirs():
        if _norm(d) == n:
            return f"{p} holds many projects; index one of them, not the whole folder"
    return None


def check_root(path: Path | str) -> Path:
    """Return the resolved path or raise UnsafeRootError with an actionable message."""
    reason = protected_reason(path)
    if reason:
        raise UnsafeRootError(
            f"refusing to index: {reason}. CodeAtlas indexes project folders only; run it inside a repository "
            f"(cd <project> && codeatlas init) or pass --repo <project>. "
            f"Set CODEATLAS_ALLOW_ANY_ROOT=1 to override deliberately.")
    return Path(path).expanduser().resolve()
