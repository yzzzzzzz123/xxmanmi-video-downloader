#!/usr/bin/env python3
"""Normal server-side WeChat login. Never exports credentials or screenshots."""
import argparse
import time
import signal
import sys
from pathlib import Path

import publisherctl as ctl
import rpa_browser as r
from server_runner import configuration, process_lock
import os


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    config = configuration(args.config)
    os.environ['MANYING_RPA_PROFILE'] = config['home'] + '/rpa-profile'
    stopping=False
    def stop(*_):
        nonlocal stopping
        stopping=True
    signal.signal(signal.SIGTERM,stop)
    signal.signal(signal.SIGINT,stop)
    def status(state,**extra):
        value={'state':state,'observed_at':ctl.now(),**extra}
        ctl.write_json(Path(config['home'])/'login-status.json',value)
        ctl.emit(value);sys.stdout.flush()
    with process_lock(Path(config['home']) / 'worker.lock'), r.browser_session(channel='chromium', headed=True) as context:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(r.HOME, wait_until='domcontentloaded', timeout=30000)
        status('waiting_for_normal_wechat_login',desktop='private SSH desktop')
        deadline = time.monotonic() + 1800
        while not stopping and time.monotonic() < deadline:
            page.wait_for_timeout(2000)
            names = page.locator('.acount_box-nickname:visible,.weui-desktop-account__nickname:visible')
            if names.count() == 1:
                if r.norm(names.inner_text()) != config['account']:
                    raise ValueError('Wrong WeChat account; expected ' + config['account'])
                status('wechat_account_verified',account=config['account'])
                return
        status('login_not_completed',note='No posting was attempted')


if __name__ == '__main__':
    main()
