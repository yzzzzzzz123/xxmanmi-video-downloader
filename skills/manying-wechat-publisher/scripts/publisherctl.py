#!/usr/bin/env python3
"""Local publishing artifacts and observation ledger; never sends WeChat posts."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import html
from html.parser import HTMLParser
import ipaddress
import json
import os
from pathlib import Path
import re
import struct
import sys
import tempfile
import unicodedata
from urllib.parse import parse_qsl, urlparse
from urllib.request import Request, urlopen

MAX_IMAGE_BYTES = 8 * 1024 * 1024
CHECK_FIELDS = ("facts", "images", "cover", "preview", "creation-source")
AUTH_KEYS = {"token", "appmsg_token", "auth_token", "ticket", "access_token", "secret", "key", "password", "code"}
TERMINAL = {"published"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def emit(value):
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_atomic(path, data):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_json(path, value):
    write_atomic(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


@contextmanager
def locked(directory):
    lock = Path(directory) / ".publisher.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ValueError("Another local writer holds this directory; inspect it before removing a stale lock") from None
    os.close(fd)
    try:
        yield
    finally:
        lock.unlink()


def public_url(value, published=False):
    parsed = urlparse(value)
    if published and parsed.fragment in {"rd", "wechat_redirect"}:
        parsed = parsed._replace(fragment="")
        value = parsed.geturl()
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Expected an HTTPS public URL without credentials")
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise ValueError("Local URLs are not public sources")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("Private addresses are not public sources")
    if any(key.lower() in AUTH_KEYS for key, _ in parse_qsl(parsed.query)):
        raise ValueError("Authentication parameters must not be saved")
    if parsed.fragment:
        raise ValueError("Use a public URL without a fragment")
    if published:
        query = dict(parse_qsl(parsed.query))
        short_link = parsed.path.startswith("/s/") and bool(parsed.path[3:].strip("/"))
        full_link = parsed.path == "/s" and all(query.get(key) for key in ("__biz", "mid", "idx", "sn"))
        if host != "mp.weixin.qq.com" or not (short_link or full_link):
            raise ValueError("Published result must be an actual public WeChat article URL")
        if any(key in {"tempkey", "is_temp_url"} for key in query):
            raise ValueError("Do not record a temporary or preview URL as published")
    return value


def safe_note(value):
    value = value.strip()
    if not value or len(value) > 1200:
        raise ValueError("Provide a short, nonempty observation note (up to 1200 characters)")
    if re.search(r"\bsk-[A-Za-z0-9._-]{12,}|(?:cookie|authorization|password|access_token)\s*[:=]", value, re.I):
        raise ValueError("Observation notes must not contain credentials")
    for url in re.findall(r"https?://[^\s<>\"']+", value):
        public_url(url)
    return value


def local_file(directory, name):
    directory = Path(directory).resolve()
    candidate = (directory / name).resolve()
    if candidate == directory or not candidate.is_relative_to(directory):
        raise ValueError("File must stay inside the article directory")
    return candidate


def load_manifest(directory, mutable=False):
    manifest = read_json(Path(directory) / "manifest.json")
    if mutable and manifest.get("schema_version") != 1:
        raise ValueError("Legacy manifest is read-only; preserve its current draft and resume it manually")
    if mutable and manifest.get("status") in TERMINAL:
        raise ValueError("Published article is read-only; do not regenerate or resubmit it")
    return manifest


def save_event(directory, manifest, event, note):
    manifest["updated_at"] = now()
    manifest["history"] = (manifest.get("history", []) + [{"at": now(), "event": event, "note": note}])[-30:]
    write_json(Path(directory) / "manifest.json", manifest)


def require_editable_content(manifest):
    if manifest["status"] in {"submitted", "awaiting_admin", "publication_unknown"}:
        raise ValueError("Resolve the in-flight publication before changing its artifacts")


def fingerprint(account, title):
    normalized = unicodedata.normalize("NFKC", title).casefold()
    normalized = "".join(c for c in normalized if c.isalnum())
    return hashlib.sha256((account.strip().casefold() + "\n" + normalized).encode("utf-8")).hexdigest()


def image_size(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24 and data[12:16] == b"IHDR":
        return (*struct.unpack(">II", data[16:24]), "png")
    if data.startswith((b"GIF87a", b"GIF89a")) and len(data) >= 13:
        return (*struct.unpack("<HH", data[6:10]), "gif")
    if data.startswith(b"\xff\xd8"):
        position = 2
        while position < len(data):
            if data[position] != 0xFF:
                position += 1
                continue
            while position < len(data) and data[position] == 0xFF:
                position += 1
            if position >= len(data):
                break
            marker = data[position]
            position += 1
            if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
                continue
            if position + 2 > len(data):
                break
            length = int.from_bytes(data[position:position + 2], "big")
            if length < 2 or position + length > len(data):
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF} and length >= 7:
                height, width = struct.unpack(">HH", data[position + 3:position + 7])
                return width, height, "jpeg"
            position += length
    raise ValueError("Asset must be a recognizable PNG, JPEG or GIF, not an HTML error page")


class NativeImage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.count = 0

    def handle_starttag(self, tag, attrs):
        if tag != "img":
            raise ValueError("Native image mapping may contain only one img tag")
        self.count += 1
        values = dict(attrs)
        for key, value in attrs:
            if not (key.startswith("data-") or key in {"src", "class", "alt", "title", "width", "height", "type", "contenteditable", "style"}):
                raise ValueError("Unexpected native image attribute")
            if key == "style" and re.search(r"url\s*\(|expression\s*\(|javascript|@import", value or "", re.I):
                raise ValueError("Unexpected active CSS in native image mapping")
            if key in {"src", "data-src"}:
                public_url(value or "")
                if urlparse(value).hostname not in {"mmbiz.qpic.cn", "mmbiz.qlogo.cn"}:
                    raise ValueError("Native image must use the actual WeChat media CDN")
        if not values.get("src"):
            raise ValueError("Native image mapping has no src")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        raise ValueError("Unexpected closing tag in native image mapping")

    def handle_data(self, data):
        if data.strip():
            raise ValueError("Native image mapping cannot include extra text")

    def handle_comment(self, data):
        raise ValueError("Native image mapping cannot include comments")


def native_html(value):
    if not isinstance(value, str) or len(value) > 12000:
        raise ValueError("Invalid native image mapping")
    parser = NativeImage()
    parser.feed(value)
    parser.close()
    if parser.count != 1:
        raise ValueError("Expected exactly one native img tag")
    return value


def new_article(args):
    root = Path(args.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    title = args.title.strip()
    if not 1 <= len(title) <= 64:
        raise ValueError("Title must be 1 to 64 characters")
    if not args.topic.strip() or not args.account.strip():
        raise ValueError("Account and topic cannot be empty")
    key = fingerprint(args.account, title)
    with locked(root):
        for path in root.glob("*/manifest.json"):
            old = read_json(path)
            if fingerprint(old.get("account", ""), old.get("title", "")) == key:
                return {"ok": True, "created": False, "duplicate": True, "dir": str(path.parent), "status": old.get("status")}
        publication_id = "manying-" + datetime.now().strftime("%Y%m%d") + "-" + key[:12]
        directory = root / publication_id
        directory.mkdir()
        manifest = {"schema_version": 1, "publication_id": publication_id, "account": args.account.strip(), "author": args.account.strip(), "topic": args.topic.strip(), "title": title, "summary": "", "status": "draft", "created_at": now(), "updated_at": now(), "dedupe_key": key, "content_file": "content.json", "body_file": "article.html", "paste_file": "paste.html", "images": [], "checks": {}, "history": []}
        write_json(directory / "manifest.json", manifest)
        write_json(directory / "content.json", {"summary": "", "sources": [], "blocks": [], "disclosure": ""})
    return {"ok": True, "created": True, "dir": str(directory), "status": "draft"}


def fetch_image(args):
    directory = Path(args.dir).resolve()
    url = public_url(args.url)
    credit = safe_note(args.credit)
    target = local_file(directory, args.file)
    if target.parent != directory or target.suffix.lower() not in {".jpg", ".jpeg", ".png", ".gif"}:
        raise ValueError("Use a PNG/JPEG/GIF filename directly in the article directory")
    with locked(directory):
        manifest = load_manifest(directory, mutable=True)
        require_editable_content(manifest)
        if target.exists() or any(i["file"] == args.file for i in manifest["images"]):
            raise ValueError("Asset already exists; inspect/reuse it instead of overwriting")
        with urlopen(Request(url, headers={"User-Agent": "Manying-Article-Assets/1.0"}), timeout=20) as response:
            public_url(response.url)
            data = response.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError("Image exceeds the bounded 8 MiB download limit")
        width, height, kind = image_size(data)
        if width <= 0 or height <= 0:
            raise ValueError("Invalid image dimensions")
        write_atomic(target, data)
        manifest["images"].append({"file": args.file, "source_url": url, "credit": credit, "sha256": hashlib.sha256(data).hexdigest(), "width": width, "height": height, "format": kind})
        manifest["checks"] = {}
        save_event(directory, manifest, "asset-downloaded", args.file)
    return {"ok": True, "file": str(target), "width": width, "height": height, "bytes": len(data)}


def register_image(args):
    directory = Path(args.dir).resolve()
    source = Path(args.path).resolve()
    target = local_file(directory, args.file)
    credit = safe_note(args.credit)
    if target.parent != directory or target.suffix.lower() not in {".jpg", ".jpeg", ".png", ".gif"}:
        raise ValueError("Use a PNG/JPEG/GIF filename directly in the article directory")
    if source.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError("Image exceeds the bounded 8 MiB asset limit")
    data = source.read_bytes()
    width, height, kind = image_size(data)
    if width <= 0 or height <= 0:
        raise ValueError("Invalid image dimensions")
    with locked(directory):
        manifest = load_manifest(directory, mutable=True)
        require_editable_content(manifest)
        if any(i["file"] == args.file for i in manifest["images"]) or (target.exists() and source != target):
            raise ValueError("Asset already exists; inspect/reuse it instead of overwriting")
        if source != target:
            write_atomic(target, data)
        manifest["images"].append({"file": args.file, "source_url": None, "source_kind": "local_asset", "credit": credit, "sha256": hashlib.sha256(data).hexdigest(), "width": width, "height": height, "format": kind})
        manifest["checks"] = {}
        save_event(directory, manifest, "asset-registered", args.file)
    return {"ok": True, "file": str(target), "width": width, "height": height, "bytes": len(data)}


def load_content(directory):
    value = read_json(Path(directory) / "content.json")
    if not isinstance(value, dict) or not isinstance(value.get("blocks"), list) or not isinstance(value.get("sources"), list):
        raise ValueError("content.json requires blocks and sources arrays")
    return value


def render_documents(directory, manifest, content):
    sources = content["sources"]
    for source in sources:
        public_url(source["url"])
    mapping_path = Path(directory) / "native-images.json"
    mapping = read_json(mapping_path) if mapping_path.exists() else {}
    if not isinstance(mapping, dict):
        raise ValueError("native-images.json must map filenames to actual native img HTML")
    available = {item["file"] for item in manifest["images"]}
    local_parts, paste_parts = [], []
    for block in content["blocks"]:
        kind = block.get("type")
        if kind in {"p", "h2"}:
            text = html.escape(str(block.get("text", "")))
            style = "font-size:19px;line-height:30px;margin:26px 0 12px;font-weight:600;" if kind == "h2" else "font-size:16px;line-height:30px;margin:0 0 18px;"
            part = f'<{kind} style="{style}">{text}</{kind}>'
            local_parts.append(part)
            paste_parts.append(part)
        elif kind == "hr":
            local_parts.append('<hr style="border:0;border-top:1px solid #ddd;margin:28px 0;">')
            paste_parts.append(local_parts[-1])
        elif kind == "image":
            name = block["file"]
            local_file(directory, name)
            if name not in available:
                raise ValueError("Every body image must be a registered asset")
            caption = html.escape(str(block.get("caption", "")))
            local_parts.append(f'<p><img src="{html.escape(name, quote=True)}" alt="{caption}" style="max-width:100%;height:auto;"></p>')
            paste_parts.append(f'<p>{native_html(mapping[name])}</p>' if name in mapping else f'<p style="color:#b00;">【待上传配图：{html.escape(name)}】</p>')
            figure_caption = f'<p style="font-size:11px;line-height:20px;color:#777;margin:6px 0 24px;">{caption}</p>'
            local_parts.append(figure_caption)
            paste_parts.append(figure_caption)
        else:
            raise ValueError("Unknown content block type")
    footer = []
    if content.get("disclosure"):
        footer.append(f'<p style="font-size:12px;line-height:24px;color:#777;">{html.escape(content["disclosure"])}</p>')
    for source in sources:
        footer.append(f'<p style="font-size:12px;line-height:24px;color:#777;">资料：{html.escape(source["title"])}</p>')
        footer.append(f'<p style="font-size:12px;line-height:24px;color:#777;word-break:break-all;">{html.escape(source["url"])}</p>')
    shell = '<section style="font-family:-apple-system,BlinkMacSystemFont,Arial,sans-serif;color:#252525;max-width:640px;margin:0 auto;">{}</section>'
    local_markup = shell.format("\n".join(local_parts + footer))
    paste_markup = shell.format("\n".join(paste_parts + footer))
    full_page = '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>' + html.escape(manifest["title"]) + '</title></head><body style="padding:16px;">' + local_markup + '</body></html>'
    return full_page, paste_markup


def render(args):
    directory = Path(args.dir).resolve()
    with locked(directory):
        manifest = load_manifest(directory, mutable=True)
        require_editable_content(manifest)
        content = load_content(directory)
        page, paste = render_documents(directory, manifest, content)
        write_atomic(directory / "article.html", page.encode("utf-8"))
        write_atomic(directory / "paste.html", paste.encode("utf-8"))
        manifest["summary"] = content.get("summary", "").strip()
        manifest["checks"] = {}
        manifest["status"] = "prepared"
        manifest["render_digest"] = hashlib.sha256((json.dumps(content, ensure_ascii=False, sort_keys=True) + paste).encode("utf-8")).hexdigest()
        save_event(directory, manifest, "rendered", "Generated local preview and paste artifacts; observation checks invalidated")
    return {"ok": True, "dir": str(directory), "status": "prepared", "needs_native_upload": "【待上传配图：" in paste}


def check(directory, stage="prepare"):
    directory = Path(directory).resolve()
    manifest = load_manifest(directory)
    if manifest.get("schema_version") != 1:
        return {"ok": False, "errors": ["Legacy manifest is read-only; inspect its existing draft manually"], "warnings": []}
    content = load_content(directory)
    errors, warnings = [], []
    if not 1 <= len(manifest.get("title", "")) <= 64:
        errors.append("Title must be 1 to 64 characters")
    if not content.get("summary", "").strip() or len(content["summary"]) > 120:
        errors.append("Summary must be 1 to 120 characters")
    if not content["sources"]:
        errors.append("At least one actually checked public source is required")
    for source in content["sources"]:
        try:
            public_url(source["url"])
            if not source.get("title") or not source.get("checked_at") or not source.get("claims"):
                errors.append("Each source requires title, checked_at and supported claims")
        except (ValueError, KeyError):
            errors.append("Invalid public source entry")
    body_images = {b["file"] for b in content["blocks"] if b.get("type") == "image"}
    if not body_images or not any(b.get("type") == "p" and b.get("text", "").strip() for b in content["blocks"]):
        errors.append("Body must contain readable paragraphs and a relevant image")
    assets = {item["file"]: item for item in manifest["images"]}
    for name in sorted(body_images):
        if name not in assets:
            errors.append("Unregistered body image: " + name)
            continue
        path = local_file(directory, name)
        if not path.is_file():
            errors.append("Missing image file: " + name)
            continue
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != assets[name]["sha256"]:
            errors.append("Image changed since registration: " + name)
        image_size(data)
        if assets[name].get("width", 0) < 640:
            warnings.append("Small image; inspect actual display quality: " + name)
    page, paste = render_documents(directory, manifest, content)
    for filename, expected in (("article.html", page), ("paste.html", paste)):
        path = directory / filename
        if not path.exists() or path.read_text(encoding="utf-8") != expected:
            errors.append("Render missing or stale: " + filename)
    if stage == "publish":
        if manifest.get("status") != "editor_saved":
            errors.append("Save the current editor version before preparing publication")
        if "【待上传配图：" in paste:
            errors.append("Native WeChat image mapping is incomplete")
        for field in CHECK_FIELDS:
            if not manifest.get("checks", {}).get(field, {}).get("verified"):
                errors.append("Missing actual observation: " + field)
        digest = hashlib.sha256((json.dumps(content, ensure_ascii=False, sort_keys=True) + paste).encode("utf-8")).hexdigest()
        if digest != manifest.get("render_digest"):
            errors.append("Content changed after render/verification")
    return {"ok": not errors, "stage": stage, "errors": errors, "warnings": warnings, "body_images": len(body_images), "body_characters": sum(len(str(b.get("text", ""))) for b in content["blocks"])}


def verify(args):
    directory = Path(args.dir).resolve()
    note = safe_note(args.note)
    with locked(directory):
        manifest = load_manifest(directory, mutable=True)
        if manifest["status"] not in {"prepared", "editor_saved"}:
            raise ValueError("Verify the prepared/current saved article, not an in-flight submission")
        result = check(directory)
        if not result["ok"]:
            raise ValueError("Preparation checks must pass before recording observation checks")
        manifest["checks"][args.field] = {"verified": True, "at": now(), "note": note}
        save_event(directory, manifest, "verified-" + args.field, note)
    return {"ok": True, "field": args.field, "status": manifest["status"]}


def record(args):
    directory = Path(args.dir).resolve()
    note = safe_note(args.note)
    with locked(directory):
        manifest = load_manifest(directory, mutable=True)
        current = manifest["status"]
        if args.event == "draft-saved":
            if current not in {"prepared", "editor_saved"} or not check(directory)["ok"]:
                raise ValueError("Only a checked prepared article can become editor_saved")
            manifest["status"] = "editor_saved"
        elif args.event == "submit-requested":
            if not check(directory, "publish")["ok"]:
                raise ValueError("Publication preparation gates have not passed")
            manifest["status"] = "submitted"
        elif args.event == "awaiting-admin":
            if current not in {"submitted", "publication_unknown", "awaiting_admin"}:
                raise ValueError("Admin verification must refer to an existing publication request")
            manifest["status"] = "awaiting_admin"
        elif args.event == "uncertain":
            if current not in {"submitted", "awaiting_admin", "publication_unknown"}:
                raise ValueError("No publication request exists to mark uncertain")
            manifest["status"] = "publication_unknown"
        elif args.event == "published":
            if current not in {"submitted", "awaiting_admin", "publication_unknown"}:
                raise ValueError("Never mark an unsubmitted draft published")
            if not args.url:
                raise ValueError("Observed public article URL is required")
            manifest["public_url"] = public_url(args.url, published=True)
            manifest["status"] = "published"
            manifest["published_at_observed"] = now()
        elif args.event == "blocked":
            if current in {"submitted", "awaiting_admin", "publication_unknown"}:
                raise ValueError("In-flight submissions must retain uncertain/awaiting state")
            manifest["status"] = "blocked"
        save_event(directory, manifest, args.event, note)
    return {"ok": True, "status": manifest["status"], "public_url": manifest.get("public_url")}


def status(args):
    root = Path(args.root).resolve()
    items = []
    for path in root.glob("*/manifest.json"):
        try:
            item = read_json(path)
            items.append({"dir": str(path.parent), "title": item.get("title"), "status": item.get("status"), "updated_at": item.get("updated_at", item.get("evidence_checked_at", "")), "legacy": item.get("schema_version") != 1, "public_url": item.get("public_url")})
        except (OSError, ValueError):
            items.append({"dir": str(path.parent), "status": "unreadable_manifest", "updated_at": ""})
    def attention(row):
        state = row.get("status") or ""
        return state in {"submitted", "awaiting_admin", "publication_unknown", "blocked", "unreadable_manifest"} or "waiting" in state or "blocked" in state

    items.sort(key=lambda row: (attention(row), row["updated_at"]), reverse=True)
    return {"ok": True, "total": len(items), "attention_required": sum(attention(row) for row in items), "articles": items[:args.limit]}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    new = sub.add_parser("new", help="Create one deduplicated local article workspace")
    new.add_argument("--root", required=True)
    new.add_argument("--account", default="漫映Manying")
    new.add_argument("--topic", required=True)
    new.add_argument("--title", required=True)
    fetch = sub.add_parser("fetch-image", help="Bounded public PNG/JPEG/GIF download; no browser/session credentials")
    fetch.add_argument("--dir", required=True)
    fetch.add_argument("--url", required=True)
    fetch.add_argument("--file", required=True)
    fetch.add_argument("--credit", required=True)
    imported = sub.add_parser("register-image", help="Register/copy an authorized local image without editing it")
    imported.add_argument("--dir", required=True)
    imported.add_argument("--path", required=True)
    imported.add_argument("--file", required=True)
    imported.add_argument("--credit", required=True)
    for name in ("render", "check"):
        command = sub.add_parser(name)
        command.add_argument("--dir", required=True)
        if name == "check":
            command.add_argument("--stage", choices=("prepare", "publish"), default="prepare")
    verified = sub.add_parser("verify", help="Record an actual editorial or visible UI observation")
    verified.add_argument("--dir", required=True)
    verified.add_argument("--field", choices=CHECK_FIELDS, required=True)
    verified.add_argument("--note", required=True)
    event = sub.add_parser("record", help="Record an observed state; never performs a publication")
    event.add_argument("--dir", required=True)
    event.add_argument("--event", choices=("draft-saved", "submit-requested", "awaiting-admin", "uncertain", "published", "blocked"), required=True)
    event.add_argument("--note", required=True)
    event.add_argument("--url")
    listing = sub.add_parser("status", help="Bounded read-only status; also accepts legacy journals")
    listing.add_argument("--root", required=True)
    listing.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    try:
        if args.command == "status" and not 1 <= args.limit <= 30:
            raise ValueError("Status limit must be between 1 and 30")
        handlers = {"new": new_article, "fetch-image": fetch_image, "register-image": register_image, "render": render, "verify": verify, "record": record, "status": status}
        result = check(args.dir, args.stage) if args.command == "check" else handlers[args.command](args)
        emit(result)
        return 0 if result.get("ok") else 2
    except (ValueError, OSError, KeyError, TypeError) as error:
        # Do not print exception strings from network libraries containing supplied URLs.
        message = str(error) if isinstance(error, ValueError) else f"Operation failed: {type(error).__name__}; inspect local inputs or public connectivity"
        emit({"ok": False, "error": message})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
