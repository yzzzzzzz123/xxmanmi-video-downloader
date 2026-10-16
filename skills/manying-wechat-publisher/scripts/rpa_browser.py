#!/usr/bin/env python3
"""Bounded Playwright draft RPA. Never submits or publishes WeChat articles."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import hashlib
from html.parser import HTMLParser
from importlib import metadata, util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit, urlunsplit

import publisherctl as ctl

HOME = "https://mp.weixin.qq.com/"
ACTIONS = {"click", "fill", "upload", "paste-html", "expect", "save"}
FIELDS = {"title", "author", "summary", "website_url"}
FORBIDDEN = re.compile(r"发表|发布|群发|定时|登录|验证码|扫码|协议|授权|支付|删除|退出|publish|send|login|delete", re.I)
AUTH_SCREEN = re.compile(r"使用微信扫码登录|扫描二维码登录|微信扫码验证|管理员扫码|安全验证|请输入验证码")


def sanitized(value):
    """Do not emit authenticated editor URLs, headers or credential-shaped text."""
    def clean_url(match):
        parsed = urlsplit(match.group(0))
        return urlunsplit((parsed.scheme, parsed.hostname or "", parsed.path, "", ""))
    value = re.sub(r"https?://[^\s<>\"']+", clean_url, str(value))
    value = re.sub(r"sk-[\w.-]{12,}", "[REDACTED]", value)
    return value[:700]


def norm(value):
    return " ".join(str(value).split())


def profile_path():
    # Browser-managed login state stays outside the repository; no cookie exports.
    configured = os.environ.get("MANYING_RPA_PROFILE")
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise ValueError("MANYING_RPA_PROFILE must be an absolute private directory")
        return path
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            raise ValueError("LOCALAPPDATA is unavailable")
        return Path(base) / "ManyingPublisher" / "rpa-profile"
    return Path.home() / ".local" / "share" / "manying-publisher" / "rpa-profile"


@contextmanager
def hidden_driver():
    """Hide only subprocesses created by this invocation, not the user's apps."""
    original = asyncio.create_subprocess_exec
    async def spawn(*args, **kwargs):
        if os.name == "nt":
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
            info = kwargs.get("startupinfo") or subprocess.STARTUPINFO()
            info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            info.wShowWindow = subprocess.SW_HIDE
            kwargs["startupinfo"] = info
        return await original(*args, **kwargs)
    asyncio.create_subprocess_exec = spawn
    try:
        yield
    finally:
        asyncio.create_subprocess_exec = original


@contextmanager
def browser_session(channel="chrome", temporary=False, headed=False):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ValueError("Playwright is not installed; no automatic installation was attempted") from None
    with hidden_driver(), sync_playwright() as runtime:
        options = {"headless": not headed, "timeout": 20000,
                   "env": {key: os.environ[key] for key in
                           ("SystemRoot", "WINDIR", "TEMP", "TMP", "PATH", "COMSPEC",
                            "HOME", "LANG", "LC_ALL", "DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR")
                           if key in os.environ}}
        proxy = os.environ.get("MANYING_RPA_PROXY")
        if proxy:
            parsed = urlsplit(proxy)
            if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
                    or not parsed.port or parsed.username or parsed.password or parsed.path not in {'', '/'}
                    or parsed.query or parsed.fragment):
                raise ValueError("RPA proxy must be a credential-free loopback HTTP endpoint")
            options['proxy'] = {'server': proxy}
        if channel == "chrome":
            options["channel"] = "chrome"
        if temporary:
            browser = runtime.chromium.launch(**options)
            context = browser.new_context()
        else:
            profile = profile_path().resolve()
            profile.mkdir(parents=True, exist_ok=True)
            context = runtime.chromium.launch_persistent_context(str(profile), **options)
            browser = None
        context.set_default_timeout(5000)
        try:
            yield context
        finally:
            context.close()
            if browser is not None:
                browser.close()


def checked_page(page, origin=HOME.rstrip("/")):
    parsed = urlsplit(page.url)
    if f"{parsed.scheme}://{parsed.netloc}" != origin:
        raise ValueError("Unexpected browser origin; no draft action allowed")
    text = page.locator("body").inner_text(timeout=5000)
    if AUTH_SCREEN.search(text) or page.locator('input[type="password"]:visible').count():
        raise ValueError("login_or_verification_required: user must complete the normal browser flow")


def one(page, selector, visible=True):
    target = page.locator(selector)
    if target.count() != 1 or (visible and not target.is_visible()):
        raise ValueError("Observed selector must resolve to exactly one available control")
    return target


def label(target):
    return norm(target.inner_text() or target.get_attribute("aria-label") or target.get_attribute("title") or target.get_attribute("value") or "")


def control_text(target):
    return target.inner_text() if target.get_attribute("contenteditable") == "true" else target.input_value()


def validate_plan(plan):
    if not isinstance(plan, dict) or plan.get("schema_version") != 1:
        raise ValueError("Expected an observed RPA plan with schema_version 1")
    account = plan.get("account", {})
    if not isinstance(account, dict) or not account.get("selector") or not account.get("text"):
        raise ValueError("Plan requires the observed account-name selector and exact text")
    if not plan.get("title_selector") or not plan.get("editor_selector"):
        raise ValueError("Plan requires observed title and body selectors to protect the existing draft")
    steps = plan.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 16:
        raise ValueError("Use one bounded plan containing 1 to 16 steps")
    for step in steps:
        if not isinstance(step, dict) or step.get("action") not in ACTIONS or not isinstance(step.get("selector"), str) or not step["selector"].strip():
            raise ValueError("Unsupported action or missing observed selector")
        action = step["action"]
        if action == "fill" and step.get("field") not in FIELDS:
            raise ValueError("Only article title, author, summary and original-link fields may be filled")
        if action in {"click", "save"}:
            text = step.get("text", "")
            if not text or FORBIDDEN.search(text):
                raise ValueError("Publication, authentication, legal and destructive clicks are not supported")
        if action == "click" and not step.get("next_selector"):
            raise ValueError("Navigation needs an observed next control for verification")
        if action == "save" and (norm(step["text"]) not in {"保存", "保存草稿", "保存为草稿"} or not step.get("receipt_selector") or not step.get("receipt_text")):
            raise ValueError("Save requires the exact draft-save button and a fresh receipt")
        if action == "expect" and not step.get("text"):
            raise ValueError("Observation step requires expected visible text")
        if action == "upload" and (not isinstance(step.get("files"), list) or not step["files"] or not step.get("ready_selector")):
            raise ValueError("Upload requires registered filenames and an observed completion control")
    if any(s["action"] == "save" for s in steps[:-1]):
        raise ValueError("Save must end a plan; verify before starting the next plan")
    return plan


class PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
    def handle_data(self, value):
        self.parts.append(value)


def execute_steps(page, plan, values, directory, progress=None, origin=HOME.rstrip("/")):
    validate_plan(plan)
    saved = False
    for index, step in enumerate(plan["steps"]):
        checked_page(page, origin)
        if label(one(page, plan["account"]["selector"])) != norm(plan["account"]["text"]):
            raise ValueError("Wrong account; stopped before changing a draft")
        action = step["action"]
        target = one(page, step["selector"], visible=action != "upload")
        if action in {"click", "save"}:
            actual_label = label(target)
            if actual_label != norm(step["text"]) or FORBIDDEN.search(actual_label):
                raise ValueError("Button changed or requires unsupported permission; no click sent")
        if action == "save":
            existing = page.locator(step["receipt_selector"])
            if existing.count() and existing.first.is_visible() and step["receipt_text"] in existing.first.inner_text():
                raise ValueError("Old save receipt is visible; cannot prove a new save")
        if action == "upload":
            if target.get_attribute("type") != "file":
                raise ValueError("Upload selector is not a real file-input control")
            files = [ctl.local_file(directory, name) for name in step["files"]]
            for name, path in zip(step["files"], files):
                if name not in values["assets"] or not path.is_file():
                    raise ValueError("Only registered current-article images may be uploaded")
        if action == "paste-html":
            markup = values["html"]
            if "【待上传配图：" in markup:
                raise ValueError("Native-image mapping is incomplete; upload and render first")
            if target.get_attribute("contenteditable") != "true":
                raise ValueError("Body must be the observed editable surface")
            parser = PlainHTML()
            parser.feed(markup)
        if action not in {"click", "expect"}:
            current_title = control_text(one(page, plan["title_selector"]))
            expected_title = values["title"]
            if current_title and norm(current_title) != norm(expected_title):
                raise ValueError("Another draft title is open; no article content will be overwritten")
            if not current_title:
                editor = one(page, plan["editor_selector"])
                empty_body = editor.evaluate("""el => {
                    const copy = el.cloneNode(true);
                    copy.querySelectorAll('.editor_content_placeholder').forEach(e => e.remove());
                    return !copy.textContent.trim() && !copy.querySelector('img:not(.ProseMirror-separator),video,iframe');
                }""")
                if action != "fill" or step.get("field") != "title" or not empty_body:
                    raise ValueError("Only a confirmed empty editor may receive the first title")
        if action != "expect" and progress:
            progress(index, "pending")  # Intent precedes a possibly irreversible UI action.
        if action == "fill":
            text = values[step["field"]]
            target.fill(text)
            if control_text(target) != text:
                raise ValueError("Filled field did not match the current article")
        elif action == "click":
            old_pages = set(page.context.pages)
            target.click()
            additions = [p for p in page.context.pages if p not in old_pages]
            if len(additions) > 1:
                raise ValueError("Multiple new pages appeared; manual target selection required")
            if additions:
                page = additions[0]
                page.wait_for_load_state("domcontentloaded")
            page.locator(step["next_selector"]).wait_for(state="visible")
        elif action == "upload":
            target.set_input_files([str(path) for path in files])
            page.locator(step["ready_selector"]).wait_for(state="visible")
        elif action == "paste-html":
            page.context.grant_permissions(["clipboard-write"], origin=origin)
            target.click()
            page.evaluate("""async data => navigator.clipboard.write([new ClipboardItem({
                'text/html': new Blob([data.html], {type: 'text/html'}),
                'text/plain': new Blob([data.plain], {type: 'text/plain'})
            })])""", {"html": markup, "plain": "\n".join(parser.parts)})
            # Normal paste, not editor internals, innerHTML or synthetic input events.
            target.press("ControlOrMeta+a")
            target.press("ControlOrMeta+v")
            proof = values["body_proof"]
            if not proof:
                raise ValueError("Pasted body was not visible in the actual editor")
            page.wait_for_function("""data => {
                const el = document.querySelector(data.selector);
                return el && el.innerText.replace(/\\s+/g, ' ').trim().includes(data.proof);
            }""", arg={"selector": step["selector"], "proof": norm(proof)}, timeout=15000)
        elif action == "expect":
            if step["text"] not in target.inner_text():
                raise ValueError("Expected UI observation is absent")
        elif action == "save":
            target.click()
            receipt = page.locator(step["receipt_selector"])
            receipt.wait_for(state="visible")
            if receipt.count() != 1 or step["receipt_text"] not in receipt.inner_text():
                raise ValueError("No unique fresh draft-save receipt")
            saved = True
        if progress:
            progress(index, "completed")
    return page, saved


def native_images(page, selector):
    if not selector:
        return []
    editor = one(page, selector)
    observations = editor.locator("img").evaluate_all("""els => els.slice(0,12).map(el => ({
        html:el.outerHTML, loaded:el.complete && el.naturalWidth>0,
        width:el.naturalWidth,height:el.naturalHeight
    }))""")
    result = []
    for observed in observations:
        try:
            ctl.native_html(observed["html"])
        except ValueError:
            result.append({"accepted_native_tag": False, "needs_review": True})
        else:
            result.append(dict(observed, accepted_native_tag=True))
    return result


def doctor(_args):
    installed = util.find_spec("playwright") is not None
    return {"ok": installed, "backend": "playwright-rpa", "uses_cua": False,
            "version": metadata.version("playwright") if installed else None,
            "profile": str(profile_path()), "publication_implemented": True,
            "publication_entry": "rpa_session.py", "run_scope": "draft_only"}


def probe(args):
    with browser_session(args.channel, args.temporary, args.headed) as context:
        page = context.pages[0] if context.pages else context.new_page()
        response = page.goto(HOME, wait_until="domcontentloaded", timeout=args.timeout_ms)
        parsed = urlsplit(page.url)
        if parsed.scheme != "https" or parsed.hostname != "mp.weixin.qq.com":
            raise ValueError("Unexpected probe origin; no account interaction attempted")
        try:
            page.wait_for_function("() => Boolean(document.body && document.body.innerText.trim())", timeout=5000)
        except Exception:
            return {"ok": False, "state": "page_not_ready", "title": sanitized(page.title()),
                    "http_status": response.status if response else None, "writes": False, "uses_cua": False}
        body = page.locator("body").inner_text(timeout=5000)
        controls = page.locator('input:not([type="password"]):visible, button:visible, [contenteditable="true"]:visible').evaluate_all("""els => els.slice(0, 20).map(el => ({
            tag:el.tagName.toLowerCase(), id:el.id, role:el.getAttribute('role'),
            placeholder:el.getAttribute('placeholder'), name:el.getAttribute('name'),
            text:el.tagName==='BUTTON' ? (el.innerText || '').slice(0,60) : null
        }))""")
        login = bool(AUTH_SCREEN.search(body) or "微信扫一扫登录" in body
                     or page.locator('input[type="password"]:visible').count())
        return {"ok": response is not None and response.status < 400,
                "state": "login_required" if login else "account_confirmation_required",
                "title": sanitized(page.title()), "http_status": response.status if response else None,
                "controls": controls, "writes": False, "uses_cua": False}


def open_for_user(args):
    with browser_session(args.channel, temporary=False, headed=True) as context:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(HOME, wait_until="domcontentloaded", timeout=args.timeout_ms)
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline and not page.is_closed():
            page.wait_for_timeout(500)
        return {"ok": True, "state": "user_session_closed", "login_verified": False,
                "note": "Run probe and verify the account before any draft action"}


def run_article(args):
    if not args.authorized_draft:
        raise ValueError("Draft editing requires current user authorization, not a skill-edit request")
    directory = Path(args.dir).resolve()
    plan = validate_plan(ctl.read_json(ctl.local_file(directory, args.plan)))
    plan_hash = hashlib.sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    state_path = directory / "rpa-state.json"
    with ctl.locked(directory):
        manifest = ctl.load_manifest(directory, mutable=True)
        ctl.require_editable_content(manifest)
        if manifest.get("status") not in {"prepared", "editor_saved"}:
            raise ValueError("Render and check the existing article before RPA")
        check = ctl.check(directory)
        if not check["ok"]:
            raise ValueError("Current article preparation checks failed")
        if norm(plan["account"]["text"]) != norm(manifest["account"]):
            raise ValueError("Plan account does not match the article account")
        previous = ctl.read_json(state_path) if state_path.exists() else {}
        if previous.get("state") in {"pending", "uncertain", "running"}:
            raise ValueError("Previous action outcome unknown; inspect the real draft, do not replay")
        if previous.get("state") == "completed" and previous.get("plan_hash") == plan_hash and previous.get("render_digest") == manifest.get("render_digest"):
            return {"ok": True, "state": "already_completed", "replayed": False}
        content = ctl.load_content(directory)
        values = {"title": manifest["title"], "author": manifest.get("author", manifest["account"]),
                  "summary": content["summary"], "website_url": content.get("website_url", "https://manying.site/"),
                  "html": (directory / "paste.html").read_text(encoding="utf-8"),
                  "assets": {item["file"] for item in manifest["images"]},
                  "body_proof": next((b["text"][:60] for b in content["blocks"] if b.get("type") == "p" and b.get("text")), "")}
        state = {"schema_version": 1, "plan_hash": plan_hash, "render_digest": manifest.get("render_digest"),
                 "state": "prepared", "completed_steps": [], "at": ctl.now()}
        def progress(index, phase):
            state.update(state="running" if phase == "completed" else phase, attempted_step=index, at=ctl.now())
            if phase == "completed":
                state["completed_steps"].append(index)
            ctl.write_json(state_path, state)
        try:
            with browser_session(args.channel, temporary=False, headed=args.headed) as context:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(HOME, wait_until="domcontentloaded", timeout=args.timeout_ms)
                page, saved = execute_steps(page, plan, values, directory, progress)
                images = native_images(page, plan.get("editor_selector"))
        except Exception:
            if state["state"] == "pending":
                state.update(state="uncertain", at=ctl.now())
                ctl.write_json(state_path, state)
            raise
        state.update(state="completed", at=ctl.now())
        ctl.write_json(state_path, state)
        if saved:
            # Still holding the article lock: record only the actual save observation.
            manifest["status"] = "editor_saved"
            ctl.save_event(directory, manifest, "draft-saved", "RPA observed the unique fresh draft-save receipt")
        return {"ok": True, "state": "draft_saved_observed" if saved else "plan_completed",
                "completed_steps": len(state["completed_steps"]), "published": False,
                "editorial_checks_recorded": False, "native_images": images}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    for name in ("probe", "open", "run"):
        item = sub.add_parser(name)
        item.add_argument("--channel", choices=("chrome", "chromium"), default="chrome")
        item.add_argument("--timeout-ms", type=int, choices=range(1000, 30001), default=15000, metavar="1000..30000")
        if name == "probe":
            item.add_argument("--temporary", action="store_true", help="Read-only anonymous probe without a persistent login profile")
        if name != "open":
            item.add_argument("--headed", action="store_true")
        if name == "open":
            item.add_argument("--seconds", type=int, choices=range(1, 601), default=180, metavar="1..600")
        if name == "run":
            item.add_argument("--dir", required=True)
            item.add_argument("--plan", required=True, help="Observed plan filename inside this article directory")
            item.add_argument("--authorized-draft", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = {"doctor": doctor, "probe": probe, "open": open_for_user, "run": run_article}[args.command](args)
        ctl.emit(result)
        return 0 if result.get("ok") else 1
    except Exception as exc:
        ctl.emit({"ok": False, "state": "blocked", "error": sanitized(exc), "published": False})
        return 1


if __name__ == "__main__":
    sys.exit(main())
