#!/usr/bin/env python3
"""Private localhost desktop for user-operated WeChat verification over SSH."""
import os
from pathlib import Path
import secrets
import signal
import subprocess
import time


def main():
    os.umask(0o077)
    root = Path(os.environ['HOME'])
    authority = root / '.Xauthority'
    subprocess.run(['xauth', '-f', str(authority), 'add', ':97', '.', secrets.token_hex(16)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    commands = [
        ['Xvfb', ':97', '-screen', '0', '1280x900x24', '-nolisten', 'tcp', '-auth', str(authority)],
        ['x11vnc', '-display', ':97', '-auth', str(authority), '-localhost', '-rfbport', '5907',
         '-nopw', '-forever', '-shared', '-quiet'],
        ['websockify', '--web=/usr/share/novnc', '127.0.0.1:6087', '127.0.0.1:5907'],
    ]
    children = []
    running = True
    def stop(*_):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        for command in commands:
            children.append(subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            time.sleep(1)
            if children[-1].poll() is not None:
                raise RuntimeError('A private desktop component exited')
        while running:
            if any(child.poll() is not None for child in children):
                raise RuntimeError('A private desktop component stopped')
            time.sleep(1)
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == '__main__':
    main()
