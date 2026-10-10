#!/usr/bin/env python3
"""CUA supplies observed URLs; this helper never crawls site HTML."""
import argparse
import collections
import contextlib
import datetime as dt
import fcntl
import functools
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.parse
import urllib.request

MANIFEST = 'manifest.json'
GIB = 1024 ** 3


class Paused(Exception):
    pass


class Conflict(Exception):
    pass


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def emit(event, **values):
    print(json.dumps(dict(event=event, time=now(), **values), ensure_ascii=False), flush=True)


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def slug(value, max_bytes=110):
    value = unicodedata.normalize('NFC', str(value))
    value = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', '_', value).strip(' .') or '未命名'
    return value.encode('utf-8')[:max_bytes].decode('utf-8', 'ignore')


def url(value):
    if not isinstance(value, str) or len(value) > 8192 or re.search(r'[\x00-\x20\x7f]', value):
        raise ValueError('Invalid URL')
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Only HTTP(S) URLs without embedded credentials are supported')
    return urllib.parse.urlunsplit(parsed._replace(fragment=''))


@functools.lru_cache(maxsize=512)
def public_host(host, port):
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(x[4][0]).is_global for x in addresses):
        raise ValueError('Non-public media host is not supported')


def public_url(value):
    value = url(value)
    parsed = urllib.parse.urlsplit(value)
    public_host(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80))
    return value


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, public_url(newurl))


def fetch_playlist(value):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), PublicRedirect())
    with opener.open(public_url(value), timeout=20) as response:
        body = response.read(4 * 1024 * 1024 + 1)
        if len(body) > 4 * 1024 * 1024:
            raise ValueError('Playlist exceeds 4 MiB')
        return body.decode('utf-8-sig'), public_url(response.url)


def playlist(value, fetch=fetch_playlist, check=public_url):
    seen = set()
    for _ in range(5):
        if value in seen:
            raise ValueError('Playlist cycle')
        seen.add(value)
        body, base = fetch(value)
        lines = [line.strip() for line in body.splitlines() if line.strip()]
        if not lines or lines[0] != '#EXTM3U':
            raise ValueError('Not an HLS playlist')
        if any(line.startswith('#EXT-X-MEDIA:') and 'TYPE=AUDIO' in line for line in lines):
            raise ValueError('External audio rendition is not supported by this helper')
        for line in lines:
            if line.startswith(('#EXT-X-KEY:', '#EXT-X-SESSION-KEY:')) and not re.search(r'(?:[:,])METHOD=NONE(?:,|$)', line):
                raise ValueError('Encrypted/DRM playlist: do not bypass')
            if not line.startswith('#'):
                check(urllib.parse.urljoin(base, line))
            for uri in re.findall(r'URI="([^"]+)"', line):
                check(urllib.parse.urljoin(base, uri))
        variants = []
        for i, line in enumerate(lines):
            if line.startswith('#EXT-X-STREAM-INF:'):
                if i + 1 == len(lines) or lines[i + 1].startswith('#'):
                    raise ValueError('Malformed master playlist')
                match = re.search(r'(?:[:,])BANDWIDTH=(\d+)', line)
                variants.append((int(match[1]) if match else 0, urllib.parse.urljoin(base, lines[i + 1])))
        if variants:
            value = max(variants)[1]
            continue
        if '#EXT-X-ENDLIST' not in lines:
            raise ValueError('Live/incomplete playlist is not supported')
        durations = [float(line.split(':', 1)[1].split(',')[0]) for line in lines if line.startswith('#EXTINF:')]
        segments = [line for line in lines if not line.startswith('#')]
        if not durations or len(durations) != len(segments) or any(not math.isfinite(x) or x <= 0 for x in durations):
            raise ValueError('Invalid segment duration/count')
        return {'url': base, 'duration': sum(durations), 'segments': len(segments)}
    raise ValueError('Playlist nesting exceeds limit')


def atomic_json(path, data):
    fd, temporary = tempfile.mkstemp(prefix='.manifest-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def new_state():
    return {'version': 1, 'created': now(), 'scope': None, 'active': [], 'entries': {},
            'coverage': {'discovered': [], 'visited': [], 'failed': {}, 'exhausted': False},
            'first_decode_verified': False}


def load_state(root):
    path = root / MANIFEST
    if path.is_symlink():
        raise Conflict('Manifest is a symlink')
    if not path.exists():
        return new_state()
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('version') != 1 or not isinstance(data.get('entries'), dict) or not isinstance(data.get('active'), list):
        raise Conflict('Unknown manifest format; leave existing file untouched')
    return data


@contextlib.contextmanager
def writer(root):
    if root.is_symlink():
        raise Conflict('Output root must not be a symlink')
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(root / '.download.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Conflict('Another ingest/run owns this output directory') from exc
        yield


def save(root, state):
    state['updated'] = now()
    atomic_json(root / MANIFEST, state)


def safe_path(root, relative):
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or any(x in ('.', '..') for x in parts):
        raise Conflict('Unsafe path in manifest')
    path = root
    for part in parts:
        path /= part
        if path.is_symlink():
            raise Conflict('Refusing symlink in output path')
    return path


def ingest(state, payload, reset=False):
    scope = payload.get('scope', state['scope'] or {'mode': 'site', 'selection': ''})
    if scope.get('mode') not in ('site', 'specified') or not isinstance(scope.get('selection', ''), str):
        raise ValueError('scope requires mode site|specified and a text selection')
    scope = {'mode': scope['mode'], 'selection': scope.get('selection', '')}
    if scope['mode'] == 'specified' and not scope['selection'].strip():
        raise ValueError('Specified mode requires an explicit selection')
    if state['scope'] and state['scope'] != scope and not reset:
        raise Conflict('Scope differs; use --new-scope to change the active queue explicitly')
    if reset:
        state['active'] = []
        state['coverage'] = new_state()['coverage']
    state['scope'] = scope
    for page in payload.get('pages', []):
        page_url = url(page['page_url'])
        title = str(page['title']).strip()
        if not title:
            raise ValueError('Page title required')
        match = re.search(r'/video(\d+)/(\d+)\.html$', urllib.parse.urlsplit(page_url).path)
        resource_id = ('%s_%s' % (match[2], match[1])) if match else hashlib.sha256(page_url.encode()).hexdigest()[:12]
        for episode in page.get('episodes', []):
            label = str(episode['label']).strip()
            if not label:
                raise ValueError('Episode label required')
            media_url = url(episode['url'])
            key = hashlib.sha256((page_url + '\n' + label).encode()).hexdigest()[:24]
            entry = state['entries'].get(key)
            if entry is None:
                # Stable suffix also prevents sanitized labels from colliding on macOS.
                entry = {'id': key, 'page_url': page_url, 'title': title, 'label': label,
                         'file': f'{slug(title)}_{resource_id}/{slug(label)}_{key[:8]}.mp4',
                         'sources': [], 'status': 'pending', 'attempts': 0, 'created': now()}
                state['entries'][key] = entry
            source = {'url': media_url, 'line': str(episode.get('line', ''))}
            if source not in entry['sources']:
                entry['sources'].append(source)
            entry['sources'].sort(key=lambda x: (x['line'] != '量子'))
            if key not in state['active']:
                state['active'].append(key)
    update = payload.get('coverage', {})
    coverage = state['coverage']
    for field in ('discovered', 'visited'):
        for item in update.get(field, []):
            item = url(item)
            if item not in coverage[field]:
                coverage[field].append(item)
    for item in coverage['visited']:
        if item not in coverage['discovered']:
            coverage['discovered'].append(item)
    for item in update.get('visited', []):
        coverage['failed'].pop(url(item), None)
    for item, reason in update.get('failed', {}).items():
        item = url(item)
        coverage['failed'][item] = str(reason)
        if item not in coverage['discovered']:
            coverage['discovered'].append(item)
    for item in update.get('resolved', []):
        coverage['failed'].pop(url(item), None)
    pending = set(coverage['discovered']) - set(coverage['visited'])
    if 'exhausted' in update and not isinstance(update['exhausted'], bool):
        raise ValueError('coverage.exhausted must be boolean')
    coverage['exhausted'] = bool(update.get('exhausted', coverage['exhausted'])) and bool(coverage['visited']) and not pending and not coverage['failed']


def space(root, floor):
    free = shutil.disk_usage(root).free
    if free < floor:
        raise Paused(f'Free space {free / GIB:.2f} GiB below floor {floor / GIB:.2f} GiB')


def direct_env():
    env = {k: v for k, v in os.environ.items() if k.lower() not in ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy')}
    env.update(NO_PROXY='*', no_proxy='*')
    return env


def ffmpeg_path(value=None):
    found = value or shutil.which('ffmpeg')
    if not found:
        try:
            import imageio_ffmpeg
            found = imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError as exc:
            raise ValueError('FFmpeg required: use --ffmpeg PATH, PATH, or install imageio-ffmpeg in a private venv') from exc
    if not Path(found).is_file() or not os.access(found, os.X_OK):
        raise ValueError('FFmpeg path is not executable')
    return str(Path(found).absolute())


def process(command, root, floor, phase):
    """Bounded output, disk checks, and no-progress timeout; no total episode timeout."""
    tail = collections.deque(maxlen=100)
    progress = {'last': time.monotonic(), 'out_time_us': None}
    fatal = collections.deque(maxlen=10)
    child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, errors='replace', env=direct_env())

    def drain(stream, is_progress):
        for line in stream:
            if is_progress and line.startswith('out_time_us='):
                value = line.strip().split('=', 1)[1]
                if value != progress['out_time_us']:
                    progress.update(last=time.monotonic(), out_time_us=value)
            elif not is_progress:
                tail.append(line.rstrip())
                if re.search(r'Failed to open segment|Error when loading first segment|HTTP error|Invalid data found', line, re.I):
                    fatal.append(line.strip())
        stream.close()

    threads = [threading.Thread(target=drain, args=(child.stdout, True), daemon=True),
               threading.Thread(target=drain, args=(child.stderr, False), daemon=True)]
    for thread in threads:
        thread.start()
    next_report = time.monotonic() + 10
    try:
        while child.poll() is None:
            space(root, floor)
            if time.monotonic() - progress['last'] > 180:
                raise RuntimeError('FFmpeg made no media progress for 180 seconds')
            if time.monotonic() >= next_report:
                emit('progress', phase=phase, out_time_us=progress['out_time_us'])
                next_report = time.monotonic() + 10
            time.sleep(0.5)
    finally:
        if child.poll() is None:
            child.send_signal(signal.SIGINT)
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        for thread in threads:
            thread.join(timeout=3)
    if child.returncode or fatal:
        raise RuntimeError(f'{phase} failed (exit {child.returncode}): ' + '\n'.join(tail)[-6000:])
    return '\n'.join(tail)


def probe(binary, file):
    result = subprocess.run([binary, '-nostdin', '-hide_banner', '-i', str(file), '-map', '0',
                             '-c', 'copy', '-t', '0', '-f', 'null', '-'],
                            capture_output=True, text=True, errors='replace', timeout=30, env=direct_env())
    match = re.search(r'Duration: (\d+):(\d+):(\d+(?:\.\d+)?)', result.stderr)
    if result.returncode or not match or 'Video:' not in result.stderr:
        raise ValueError('Output does not probe as a finite video file: ' + result.stderr[-1000:])
    return {'duration': int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3]),
            'video': True, 'audio': 'Audio:' in result.stderr}


def matches(path, verified):
    return (path.is_file() and not path.is_symlink() and
            path.stat().st_size == verified.get('size') and digest(path) == verified.get('sha256'))


def complete_file(root, state, entry, part, final):
    if final.exists():
        if not matches(final, entry['verified']):
            raise Conflict('Existing final file differs; not overwriting')
    else:
        os.link(part, final)  # Atomic no-clobber promotion, same filesystem.
    if part.exists() and matches(part, entry['verified']):
        part.unlink()
    entry.update(status='complete', completed=now(), owned_part=False)
    entry.pop('error', None)
    if entry['verified'].get('full_decode'):
        state['first_decode_verified'] = True
    save(root, state)


def handle_entry(root, state, entry, binary, floor):
    final = safe_path(root, entry['file'])
    part = safe_path(root, entry['file'][:-4] + '.part.mp4')
    if entry.get('verified') and final.exists():
        if not matches(final, entry['verified']):
            raise Conflict('Completed/staged file changed; preserve it for inspection')
        if entry['status'] != 'complete':
            complete_file(root, state, entry, part, final)
        return 'skipped'
    if entry['status'] == 'complete':
        raise Conflict('Completed file is missing; do not silently redownload')
    if final.exists() or (part.exists() and not entry.get('owned_part')):
        raise Conflict('Unknown final/partial file exists; not overwriting')
    space(root, floor)
    final.parent.mkdir(parents=True, exist_ok=True)
    # An identical URL already verified elsewhere is an alias, not another download.
    own_urls = {source['url'] for source in entry['sources']}
    for other in state['entries'].values():
        if other['id'] != entry['id'] and other['status'] == 'complete' and own_urls.intersection(s['url'] for s in other['sources']):
            other_file = safe_path(root, other['file'])
            if matches(other_file, other.get('verified', {})):
                entry.update(status='duplicate', duplicate_of=other['id'])
                save(root, state)
                return 'duplicate'
    sources = list(dict.fromkeys(source['url'] for source in entry['sources']))
    # At most 3 attempts per invocation; prioritize 量子, then observed alternatives.
    for attempt in range(3):
        try:
            space(root, floor)
            source = sources[attempt % len(sources)]
            entry.update(status='checking', attempts=entry['attempts'] + 1)
            save(root, state)
            media = playlist(source)
            if part.exists():
                if not entry.get('owned_part') or part.is_symlink():
                    raise Conflict('Partial file is not owned by this queue')
                part.unlink()  # Only this manifest-owned incomplete episode is restarted.
            entry.update(status='downloading', owned_part=True,
                         chosen_source=source, media=media, started=now())
            entry.pop('verified', None)
            save(root, state)
            emit('download', id=entry['id'], title=entry['title'], episode=entry['label'], attempt=attempt + 1)
            process([binary, '-nostdin', '-n', '-hide_banner', '-loglevel', 'warning', '-nostats',
                     '-stats_period', '2', '-progress', 'pipe:1', '-protocol_whitelist', 'http,https,tcp,tls',
                     '-http_proxy', '', '-rw_timeout', '20000000', '-i', media['url'],
                     '-map', '0:v:0', '-map', '0:a:0?', '-c', 'copy', '-movflags', '+faststart', str(part)],
                    root, floor, 'download')
            metadata = probe(binary, part)
            if abs(metadata['duration'] - media['duration']) > max(2.0, media['duration'] * 0.0005):
                raise ValueError(f'Duration mismatch: manifest {media["duration"]:.3f}s vs MP4 {metadata["duration"]:.3f}s')
            full_decode = not state['first_decode_verified']
            if full_decode:
                emit('verify', id=entry['id'], check='full_decode')
                process([binary, '-nostdin', '-hide_banner', '-loglevel', 'warning', '-xerror', '-nostats',
                         '-stats_period', '2', '-progress', 'pipe:1', '-i', str(part), '-map', '0:v:0',
                         '-map', '0:a:0?', '-fps_mode', 'passthrough', '-enc_time_base:v', 'demux', '-f', 'null', '-'],
                        root, floor, 'full_decode')
            entry['verified'] = dict(metadata, size=part.stat().st_size, sha256=digest(part), full_decode=full_decode)
            entry['status'] = 'promoting'
            save(root, state)
            complete_file(root, state, entry, part, final)
            return 'complete'
        except (Paused, Conflict, KeyboardInterrupt):
            raise
        except Exception as exc:
            entry.update(error=str(exc)[-6000:], status='failed')
            save(root, state)
            emit('attempt_failed', id=entry['id'], attempt=attempt + 1, error=str(exc)[-2000:])
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    return 'failed'


def summary(state):
    counts = collections.Counter(state['entries'][key]['status'] for key in state['active'])
    coverage = state['coverage']
    pending = sorted(set(coverage['discovered']) - set(coverage['visited']))
    queue_complete = bool(state['active']) and all(state['entries'][key]['status'] in ('complete', 'duplicate') for key in state['active'])
    return {'scope': state['scope'], 'active_episodes': len(state['active']), 'counts': dict(counts),
            'coverage': dict(coverage, pending=pending), 'queue_complete': queue_complete,
            'scope_complete': queue_complete and coverage['exhausted'] and not pending and not coverage['failed']}


def run(root, state, binary, floor, limit, retry_failed=False):
    processed = 0
    for key in state['active']:
        entry = state['entries'][key]
        if entry['status'] in ('failed', 'conflict') and not retry_failed:
            continue
        if limit and processed >= limit:
            break
        try:
            if entry['status'] == 'duplicate':
                other = state['entries'].get(entry.get('duplicate_of'), {})
                if other.get('status') == 'complete' and matches(safe_path(root, other['file']), other.get('verified', {})):
                    continue
                raise Conflict('Duplicate target is missing or changed')
            outcome = handle_entry(root, state, entry, binary, floor)
            if outcome != 'skipped':
                processed += 1
            emit(outcome, id=key)
        except (Paused, KeyboardInterrupt) as exc:
            entry.update(status='paused', error=str(exc) or 'Interrupted; incomplete episode restarts on resume')
            save(root, state)
            emit('paused', id=key, reason=entry['error'])
            return 2
        except Conflict as exc:
            entry.update(status='conflict', error=str(exc))
            save(root, state)
            processed += 1
            emit('conflict', id=key, error=str(exc))
    emit('status', **summary(state))
    return 3 if any(state['entries'][key]['status'] in ('failed', 'conflict') for key in state['active']) else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True,
                        help='Explicit task workspace outside the final video library')
    sub = parser.add_subparsers(dest='command', required=True)
    add = sub.add_parser('ingest', help='Read CUA observation JSON from stdin')
    add.add_argument('--new-scope', action='store_true')
    execute = sub.add_parser('run', help='Resume serial downloads; does not browse/discover pages')
    execute.add_argument('--ffmpeg')
    execute.add_argument('--min-free-gib', type=float, default=10)
    execute.add_argument('--limit', type=int, default=0, help='0 = all currently queued episodes')
    execute.add_argument('--retry-failed', action='store_true')
    sub.add_parser('status', help='Read atomic manifest without waiting for a running downloader')
    args = parser.parse_args()
    root = args.output.expanduser().absolute()
    if args.command == 'status':
        emit('status', **summary(load_state(root)))
        return 0
    with writer(root):
        state = load_state(root)
        if args.command == 'ingest':
            raw = sys.stdin.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                raise ValueError('Ingest exceeds 16 MiB')
            ingest(state, json.loads(raw), args.new_scope)
            save(root, state)
            emit('ingested', **summary(state))
            return 0
        if not math.isfinite(args.min_free_gib) or args.min_free_gib <= 0 or args.limit < 0:
            raise ValueError('Use positive disk floor and nonnegative limit')
        binary = ffmpeg_path(args.ffmpeg)
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        return run(root, state, binary, args.min_free_gib * GIB, args.limit, args.retry_failed)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        emit('paused', reason='Interrupted outside active download; existing manifest preserved')
        sys.exit(2)
    except Exception as error:
        emit('error', error=str(error))
        sys.exit(1)
