"""Read-only source checks and a portable source archive; no runtime imports."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import unquote, urlsplit
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
NAME = "xxmanmi-video-downloader"
ROOT_FILES = (".gitignore", "AGENTS.md", "README.md", "LICENSE", "requirements.txt")
SOURCE_DIRS = ("scripts", "references", "skills")
EXCLUDED_DIRS = {".git", ".runtime", ".secrets", ".venv", "venv", "__pycache__",
                 "node_modules", "claims", "downloads", "observations", "dist", "build"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".log", ".dump", ".bak", ".key", ".pem",
                     ".pfx", ".p12", ".zip", ".gz", ".tar", ".7z", ".sqlite",
                     ".db", ".mp4", ".mkv", ".ts", ".m3u8", ".ndjson", ".jsonl"}
EXCLUDED_FILES = {"config.local.json", "manifest.json", "postgres_password.txt"}
REQUIRED = ("README.md", "AGENTS.md", "LICENSE", "requirements.txt",
            "skills/xxmanmi-video-downloader/SKILL.md",
            "scripts/download.py", "scripts/collection/shared_discovery.py")


def source_files():
    """Include formal untracked source too; do not traverse runtime or links."""
    paths = [PROJECT / name for name in ROOT_FILES if (PROJECT / name).is_file()]
    for name in SOURCE_DIRS:
        for current, directories, filenames in os.walk(PROJECT / name, followlinks=False):
            directories[:] = sorted(d for d in directories if d not in EXCLUDED_DIRS
                                     and not d.startswith(".")
                                     and not (Path(current) / d).is_symlink()
                                     and not getattr(Path(current) / d, "is_junction", lambda: False)())
            for filename in sorted(filenames):
                path = Path(current) / filename
                if (filename in EXCLUDED_FILES or path.suffix.lower() in EXCLUDED_SUFFIXES
                        or (filename.startswith(".") and filename not in {".gitignore", ".env.example"})):
                    continue
                paths.append(path)
    result = {}
    for path in sorted(paths):
        if path.is_symlink() or PROJECT not in path.resolve().parents:
            raise ValueError(f"Source link is not packaged: {path.relative_to(PROJECT)}")
        result[path.relative_to(PROJECT).as_posix()] = path.read_bytes()
    return result


def check(files):
    errors = [f"Missing required source: {name}" for name in REQUIRED if name not in files]
    if (PROJECT / "SKILL.md").exists():
        errors.append("Keep the single main skill in skills/xxmanmi-video-downloader; no root entry")
    python_count = skill_count = link_count = 0
    for name, data in files.items():
        if name.endswith(".py"):
            python_count += 1
            try:
                ast.parse(data, filename=name)
            except (SyntaxError, ValueError) as exc:
                errors.append(f"Python syntax: {name}: {exc}")
        if name.endswith(".json"):
            try:
                json.loads(data)
            except ValueError as exc:
                errors.append(f"JSON syntax: {name}: {exc}")
        if name.startswith("skills/") and name.endswith("/SKILL.md"):
            skill_count += 1
            content = data.decode("utf-8-sig")
            front = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", content, re.S)
            found = re.search(r"^name:\s*([a-z0-9-]+)\s*$", front[1], re.M) if front else None
            description = re.search(r"^description:\s*\S", front[1], re.M) if front else None
            if not found or found[1] != Path(name).parent.name or not description:
                errors.append(f"Invalid skill frontmatter: {name}")
        if name.endswith(".md"):
            content = data.decode("utf-8-sig")
            for target in re.findall(r"\[[^\]\n]+\]\(([^)\n]+)\)", content):
                target = target.strip()
                target = target[1:target.index(">")] if target.startswith("<") and ">" in target else target.split()[0]
                if urlsplit(target).scheme or target.startswith("#") or "$" in target:
                    continue
                relative = unquote(target.split("#", 1)[0])
                path = (PROJECT / name).parent / relative
                resolved = path.resolve()
                link_count += 1
                if resolved != PROJECT and PROJECT not in resolved.parents:
                    errors.append(f"Link leaves project: {name} -> {target}")
                else:
                    key = resolved.relative_to(PROJECT).as_posix()
                    if key not in files and not any(p.startswith(key.rstrip("/") + "/") for p in files):
                        errors.append(f"Link target absent from package: {name} -> {target}")
        if Path(name).name.startswith("requirements") and name.endswith(".txt"):
            for include in re.findall(r"^-r\s+(\S+)", data.decode("utf-8-sig"), re.M):
                path = ((PROJECT / name).parent / include).resolve()
                if PROJECT not in path.parents or path.relative_to(PROJECT).as_posix() not in files:
                    errors.append(f"Missing requirements include: {name} -> {include}")
    for directory in sorted((PROJECT / "skills").iterdir()):
        prefix = f"skills/{directory.name}/"
        if (directory.is_dir() and not directory.name.startswith(".")
                and any(name.startswith(prefix) for name in files)
                and not (directory / "SKILL.md").is_file()):
            errors.append(f"Skill folder without entry: skills/{directory.name}")
    html = files.get("web/static/index.html", b"").decode("utf-8-sig")
    for asset in re.findall(r'(?:src|href)=["\'](/static/[^"\'?]+)', html):
        if "web" + asset not in files:
            errors.append(f"Missing static asset: {asset}")
    if errors:
        raise ValueError("\n".join(errors))
    return {"ok": True, "files": len(files), "python_files": python_count,
            "skills": skill_count, "relative_links": link_count,
            "source_bytes": sum(map(len, files.values()))}


def package(files, output):
    output = output.expanduser().absolute()
    if output.suffix.lower() != ".zip":
        raise ValueError("Package output must be a .zip file")
    if output.exists():
        raise FileExistsError(f"Package already exists; choose a new --output: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".source-package-", suffix=".tmp", dir=output.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    manifest = "".join(f"{digest}  {name}\n" for name, digest in hashes.items()).encode("utf-8")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for name, data in {**files, "MANIFEST.sha256": manifest}.items():
                info = zipfile.ZipInfo(f"{NAME}/{name}", date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (0o100755 if name.endswith(".sh") else 0o100644) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, data)
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None or len(archive.namelist()) != len(files) + 1:
                raise ValueError("Package archive validation failed")
            for name, digest in hashes.items():
                if hashlib.sha256(archive.read(f"{NAME}/{name}")).hexdigest() != digest:
                    raise ValueError(f"Package checksum mismatch: {name}")
        # Publish a complete verified archive without overwriting a racing writer.
        os.link(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return {"package": str(output), "package_bytes": output.stat().st_size,
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check")
    build = commands.add_parser("package")
    build.add_argument("--output", type=Path, default=PROJECT / "dist" / f"{NAME}.zip")
    args = parser.parse_args()
    try:
        files = source_files()
        result = check(files)
        if args.command == "package":
            result.update(package(files, args.output))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
