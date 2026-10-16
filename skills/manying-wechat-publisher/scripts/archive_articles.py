#!/usr/bin/env python3
"""Daily local archive and duplicate review; never talks to WeChat."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import unicodedata

import publisherctl as ctl

SHANGHAI = timezone(timedelta(hours=8))


def normalize(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', text).casefold() if c.isalnum())


def body_parts(content):
    # Shared website introductions, captions, disclosures and sources are not article ideas.
    return [b.get('text', '') for b in content.get('blocks', [])
            if b.get('type') in {'p', 'h2'} and 'manying.site' not in b.get('text', '')]


def article_record(directory):
    manifest = ctl.load_manifest(directory)
    content_path = directory / manifest.get('content_file', 'content.json')
    content = ctl.read_json(content_path) if content_path.exists() else None
    editorial_path = directory / 'editorial.json'
    editorial = (content or {}).get('editorial') or (
        ctl.read_json(editorial_path) if editorial_path.exists() else {})
    text = '\n\n'.join(body_parts(content or {}))
    record = {
        'publication_id': manifest.get('publication_id', directory.name),
        'account': manifest.get('account'), 'title': manifest.get('title', ''),
        'topic': manifest.get('topic', ''), 'status': manifest.get('status'),
        'published_at': manifest.get('published_at'),
        'published_at_observed': manifest.get('published_at_observed'),
        'public_url': manifest.get('public_url'), 'provenance': 'local_article',
        'article_directory': directory.name, 'content': content,
        'editorial': editorial, 'body_sha256': hashlib.sha256(normalize(text).encode()).hexdigest() if text else None,
        'images': [{k: i.get(k) for k in ('file', 'sha256', 'format', 'width', 'height', 'source_url', 'credit')}
                   for i in manifest.get('images', [])],
    }
    if record['public_url']:
        ctl.public_url(record['public_url'], published=True)
    for image in record['images']:
        if image.get('source_url'):
            ctl.public_url(image['source_url'])
    for source in (content or {}).get('sources', []):
        ctl.public_url(source['url'])
    return record


def all_records(root):
    records = [article_record(p.parent) for p in sorted(root.glob('*/manifest.json'))]
    backend_file = root / 'archive' / 'backend-only.json'
    if backend_file.exists():
        for row in ctl.read_json(backend_file):
            ctl.public_url(row['public_url'], published=True)
            # Only fields actually observed in the backend; no invented local body.
            records.append({k: row.get(k) for k in ('publication_id', 'account', 'title', 'public_url', 'published_at')}
                           | {'status': 'published', 'provenance': 'backend_only', 'content': None, 'editorial': {}, 'images': []})
    # Daily snapshots remain useful when a source folder was moved or cleaned later.
    known = {r['publication_id']: r for r in records}
    for path in sorted((root / 'archive').glob('????-??-??.json')):
        for row in ctl.read_json(path).get('articles', []):
            if row['publication_id'] not in known:
                ctl.public_url(row['public_url'], published=True)
                known[row['publication_id']] = row
    return list(known.values())


def publication_day(record):
    value = record.get('published_at') or record.get('published_at_observed')
    if not value:
        return None
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('Publication timestamps require an explicit timezone')
    return stamp.astimezone(SHANGHAI).date().isoformat()


def archive_day(root, day):
    date.fromisoformat(day)
    rows = [r for r in all_records(root) if r['status'] == 'published' and publication_day(r) == day]
    rows.sort(key=lambda r: r.get('published_at') or r.get('published_at_observed') or '')
    target = root / 'archive' / (day + '.json')
    target.parent.mkdir(parents=True, exist_ok=True)
    with ctl.locked(target.parent):
        ctl.write_json(target, {'schema_version': 1, 'date': day, 'timezone': 'Asia/Shanghai',
                                'updated_at': ctl.now(), 'articles': rows})
    return {'ok': True, 'file': str(target), 'date': day, 'articles': len(rows),
            'full_text_archived': sum(r['content'] is not None for r in rows),
            'backend_only': sum(r['provenance'] == 'backend_only' for r in rows)}


def check_article(root, directory):
    if not directory.is_relative_to(root):
        raise ValueError('Current article must be inside the article root')
    current = article_record(directory)
    current_parts = [normalize(p) for p in body_parts(current['content'] or {})]
    body = ''.join(current_parts)
    if not body:
        raise ValueError('Current article has no substantive body')
    current_hashes = {i['sha256'] for i in current['images'] if i.get('sha256')}
    duplicates, review = [], []
    compared = 0
    for old in all_records(root):
        if old['publication_id'] == current['publication_id']:
            continue
        if current.get('account') and old.get('account') and old['account'] != current['account']:
            continue
        compared += 1
        reasons, clues = [], []
        if normalize(current['title']) == normalize(old['title']):
            reasons.append('same_title')
        old_parts = [normalize(p) for p in body_parts(old['content'] or {})]
        old_body = ''.join(old_parts)
        if old_body and body == old_body:
            reasons.append('same_body')
        shared = [p for p in current_parts if len(p) >= 40 and p in old_parts]
        if shared:
            reasons.append('repeated_substantive_paragraph')
        ratio = SequenceMatcher(None, body, old_body, autojunk=False).ratio() if old_body else 0
        if ratio >= 0.72 and 'same_body' not in reasons:
            clues.append({'body_similarity': round(ratio, 3)})
        reused = current_hashes & {i.get('sha256') for i in old['images']}
        if reused:
            clues.append({'shared_images': len(reused)})
        if reasons:
            duplicates.append({'title': old['title'], 'reasons': reasons})
        if clues:
            review.append({'title': old['title'], 'clues': clues, 'editorial': old['editorial']})
    return {'ok': not duplicates, 'compared': compared, 'duplicates': duplicates[:10],
            'review': review[:10], 'editorial_review_required': True,
            'note': 'Exact duplicate checks supplement human review of topic, angle, depth and pace.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    daily = commands.add_parser('archive')
    daily.add_argument('--root', required=True)
    daily.add_argument('--date', default=datetime.now(SHANGHAI).date().isoformat())
    check = commands.add_parser('check')
    check.add_argument('--root', required=True)
    check.add_argument('--dir', required=True)
    args = parser.parse_args()
    try:
        root = Path(args.root).resolve()
        if not root.is_dir():
            raise ValueError('Article root does not exist')
        result = archive_day(root, args.date) if args.command == 'archive' else check_article(root, Path(args.dir).resolve())
        ctl.emit(result)
        return 0 if result['ok'] else 2
    except (ValueError, OSError, KeyError, TypeError) as error:
        ctl.emit({'ok': False, 'error': str(error) if isinstance(error, ValueError) else type(error).__name__})
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
