#!/usr/bin/env python3
"""Local, persistent Playwright RPA. One JSON request per line; no browser tool."""
from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import publisherctl as ctl
import rpa_browser as r
from wechat_rpa import Session, INFLIGHT, redact, require


def dispatch(session, request):
    require(isinstance(request, dict), 'Expected a JSON object')
    op = request.get('op')
    if op == 'reload-local-code':
        if session.directory:
            require(session.manifest()['status'] not in INFLIGHT,
                    'Do not reload code during an in-flight publication or verification')
        module = importlib.reload(sys.modules[Session.__module__])
        session.__class__ = module.Session
        return {'reloaded': True, 'same_browser_context': True,
                'note': 'Reloaded only the installed local RPA implementation; no arbitrary code accepted'}
    methods = {
        'article': session.article, 'tabs': session.tabs, 'use': session.use,
        'observe': session.observe, 'dom': session.dom, 'screenshot': session.screenshot,
        'click': session.click, 'hover': session.hover, 'pointer': session.pointer,
        'prepare': session.prepare, 'cover-select': session.cover_select,
        'cover-confirm': session.cover_confirm, 'original': session.original,
        'links-source': session.links_source, 'save': session.save,
        'open-preview': session.open_preview, 'readcheck': session.readcheck,
        'publication-open': session.publication_open,
        'publication-settings': session.publication_settings,
        'publication-config': session.publication_config,
        'publication-review': session.publication_review, 'submit': session.submit,
        'history': session.history, 'open-public': session.open_public,
        'record-published': session.record_published,
    }
    if op == 'wait':
        milliseconds = request.get('milliseconds', 1000)
        require(isinstance(milliseconds, int) and 0 <= milliseconds <= 5000, 'Use a bounded 0..5000 ms UI wait')
        session.page.wait_for_timeout(milliseconds)
        return {'waited_ms': milliseconds}
    if op == 'quit':
        if session.directory:
            manifest = session.manifest()
            require(manifest['status'] != 'awaiting_admin' or request.get('close') is True,
                    'Administrator verification is pending; keep this session open or explicitly request close')
        return {'closed': True}
    require(op in methods, 'Unsupported RPA operation; arbitrary code/HTTP/authentication actions are not accepted')
    args = {k: v for k, v in request.items() if k not in {'op', 'id'}}
    value = methods[op](**args)
    if op == 'observe' and session.directory:
        text = session.page.locator('body').inner_text()
        if r.AUTH_SCREEN.search(text) or '微信扫一扫登录' in text:
            manifest = session.manifest()
            if manifest['status'] in INFLIGHT and any(x in text for x in ('管理员', '微信扫码验证', '安全验证')):
                ctl.record(NS(dir=str(session.directory), event='awaiting-admin',
                           note='当前真实发表验证页要求用户微信扫码/安全验证；保留当前会话，不重发', url=None))
            return {'state': 'user_verification_required', 'page_preserved': True,
                    'note': 'User must complete the normal visible WeChat verification; do not save QR/authentication URLs'}
    return value


def serve(context, root, args, *, input_stream=None, output_stream=None, navigate=True):
    input_stream, output_stream = input_stream or sys.stdin, output_stream or sys.stdout
    session = Session(context, root, authorized_draft=args.authorized_draft,
                      authorized_publish=args.authorized_publish, authorized_notify=args.authorized_notify)
    def emit(value):
        print(json.dumps(redact(value), ensure_ascii=False, separators=(',', ':')), file=output_stream, flush=True)
    if navigate:
        try:
            session.page.goto(r.HOME, wait_until='domcontentloaded', timeout=20000)
        except Exception as exc:
            emit({'ok': False, 'state': 'navigation_unconfirmed', 'error': r.sanitized(exc), 'observe_next': True})
    emit({'ok': True, 'state': 'session_ready', 'backend': 'local-playwright-rpa',
          'uses_browser_tool': False, 'headed': args.headed})
    for line in input_stream:
        if not line.strip():
            continue
        request = {}
        try:
            request = json.loads(line)
            result = dispatch(session, request)
            emit({'ok': True, 'id': request.get('id'), 'op': request.get('op'), 'result': result})
            if request.get('op') == 'quit':
                break
        except Exception as exc:
            # Do not print Playwright tracebacks containing current authenticated editor URLs.
            emit({'ok': False, 'id': request.get('id') if isinstance(request, dict) else None,
                  'state': 'blocked', 'error': r.sanitized(exc), 'observe_before_next_action': True})
    return session


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, help='Existing article root; reuse original article directories')
    parser.add_argument('--channel', choices=('chrome', 'chromium'), default=os.environ.get('MANYING_RPA_CHANNEL', 'chrome'))
    parser.add_argument('--headed', action='store_true', default=os.environ.get('MANYING_RPA_HEADED') == '1', help='Interactive Chrome for normal user login/verification')
    parser.add_argument('--authorized-draft', action='store_true')
    parser.add_argument('--authorized-publish', action='store_true')
    parser.add_argument('--authorized-notify', action='store_true')
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    try:
        require(root.is_dir(), '--root must be an existing article directory')
        with r.browser_session(channel=args.channel, headed=args.headed) as context:
            serve(context, root, args)
        return 0
    except Exception as exc:
        ctl.emit({'ok': False, 'state': 'blocked', 'error': r.sanitized(exc)})
        return 1


if __name__ == '__main__':
    sys.exit(main())
