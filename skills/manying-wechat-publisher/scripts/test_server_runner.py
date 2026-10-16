"""Scheduler recovery and independently checked publication receipts, without real posting."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import publisherctl as ctl
import rpa_browser as r
import server_runner as s


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = dict(home=str(self.root), articles=str(self.root/'articles'),
                           workspace=str(self.root/'workspace'), codex=str(self.root/'codex'),
                           times=['12:00', '18:30'], count=3, timezone='Asia/Shanghai',
                           notify=False, account='漫映Manying', enabled=True)
        (self.root/'articles').mkdir()
        self.noon = datetime(2026, 10, 14, 4, tzinfo=timezone.utc)

    def tearDown(self):
        self.temp.cleanup()

    def test_beijing_times_no_early_or_stale_catchup(self):
        self.assertEqual(s.due_slot(self.noon, self.config), '20261014-1200')
        self.assertIsNone(s.due_slot(self.noon-timedelta(seconds=1), self.config))
        self.assertIsNone(s.due_slot(self.noon+timedelta(minutes=10), self.config))
        self.assertEqual(s.due_slot(self.noon+timedelta(hours=6, minutes=30), self.config), '20261014-1830')
        self.assertIsNone(s.due_slot(self.noon+timedelta(days=1, hours=1), self.config))

    def test_one_claim_survives_login_failure_without_auto_replay(self):
        with patch.object(s, 'preflight', return_value=('waiting_model_login','login needed')) as doctor:
            first = s.run(self.config, now=self.noon)
            self.assertEqual(first['state'], 'waiting_model_login')
            self.assertEqual(s.run(self.config, now=self.noon)['state'], 'slot_already_claimed')
            self.assertEqual(doctor.call_count, 1)
            self.assertEqual(s.run(self.config, resume='20261014-1200')['state'], 'waiting_model_login')
            self.assertEqual(doctor.call_count, 2)

    def test_crashed_running_batch_blocks_next_slot(self):
        p=self.root/'jobs'/'20261014-1200'/'job.json';p.parent.mkdir(parents=True)
        ctl.write_json(p, dict(id='20261014-1200', state='running'))
        with patch.object(s, 'preflight') as doctor:
            self.assertEqual(s.run(self.config, now=self.noon+timedelta(hours=6,minutes=30))['state'],
                             'blocked_by_existing_batch')
            doctor.assert_not_called()

    def test_live_lock_excludes_another_worker_and_releases_after_exit(self):
        with s.process_lock(self.root/'worker.lock'):
            with self.assertRaises(OSError):
                with s.process_lock(self.root/'worker.lock'):
                    pass
        with s.process_lock(self.root/'worker.lock'):
            pass

    def test_doctor_never_reopens_profile_owned_by_login_or_publication(self):
        with s.process_lock(self.root/'worker.lock'),patch.object(s,'preflight') as check:
            self.assertEqual(s.doctor(self.config)['state'],'busy')
            check.assert_not_called()
        with patch.object(s,'preflight',return_value=None):
            self.assertTrue(s.doctor(self.config)['ready'])

    def test_manual_direct_batch_is_claimed_once_and_route_survives_resume(self):
        seen=[]
        def preflight(config):
            seen.append(os.environ.get('MANYING_RPA_PROXY'))
            return ('waiting_wechat_login','normal login needed')
        with patch.dict(os.environ,{'MANYING_RPA_PROXY':'http://127.0.0.1:17897'}),patch.object(s,'preflight',side_effect=preflight):
            job=s.run(self.config,manual='20261014-1628',network='direct')
            self.assertEqual(job['network'],'direct')
            self.assertEqual(job['trigger'],'manual')
            self.assertEqual(job['count'],3)
            self.assertEqual(os.environ['MANYING_RPA_PROXY'],'http://127.0.0.1:17897')
            self.assertEqual(s.run(self.config,manual='20261014-1628',network='direct')['state'],'slot_already_claimed')
            s.run(self.config,resume='20261014-1628')
            self.assertEqual(seen,[None,None])

    def test_manual_request_does_not_bypass_unknown_previous_batch(self):
        p=self.root/'jobs'/'20261014-1200'/'job.json';p.parent.mkdir(parents=True)
        ctl.write_json(p,dict(id='20261014-1200',state='attention'))
        with patch.object(s,'launch') as launch:
            self.assertEqual(s.run(self.config,manual='20261014-1628',network='direct')['state'],'blocked_by_existing_batch')
            launch.assert_not_called()

    def fixture(self, name):
        d=self.root/'articles'/name;d.mkdir()
        m=dict(title=name,status='published',platform_status='已发表',render_digest='abc',
               public_url='https://mp.weixin.qq.com/s/'+name)
        ctl.write_json(d/'manifest.json',m)
        (d/'wechat-public.png').write_bytes(b'public screenshot fixture')
        p=dict(title=name,public_url=m['public_url'],render_digest='abc',backend_status='已发表',
               paragraphs_match=True,source_visible=True,images=[dict(loaded=True,width=1080)],
               screenshot_sha256=hashlib.sha256((d/'wechat-public.png').read_bytes()).hexdigest())
        ctl.write_json(d/'public-verification.json',p)
        return d,m,p

    def test_process_exit_and_manifest_alone_do_not_prove_publication(self):
        d,m,p=self.fixture('one')
        (d/'public-verification.json').unlink()
        self.assertFalse(s.proof_for(d,m))
        self.assertEqual(s.result(self.config,dict(before=[],count=3))[0],'attention')

    def test_three_current_receipts_required_old_posts_not_counted(self):
        self.fixture('old')
        for name in ['one','two','three']:self.fixture(name)
        state,_,posts=s.result(self.config,dict(before=['old'],count=3))
        self.assertEqual(state,'completed');self.assertEqual(len(posts),3)
        (self.root/'articles'/'two'/'wechat-public.png').write_bytes(b'changed screenshot')
        self.assertEqual(s.result(self.config,dict(before=['old'],count=3))[0],'attention')

    def test_wrong_digest_loading_or_preview_url_rejected(self):
        d,m,p=self.fixture('one')
        for change in [dict(render_digest='different'), dict(paragraphs_match=False),
                       dict(images=[dict(loaded=False,width=1080)]),
                       dict(public_url='https://mp.weixin.qq.com/s?tempkey=not-public')]:
            ctl.write_json(d/'public-verification.json',{**p,**change})
            self.assertFalse(s.proof_for(d,m))

    def test_profile_override_must_be_absolute(self):
        with patch.dict(os.environ, {'MANYING_RPA_PROFILE':'relative-profile'}):
            with self.assertRaises(ValueError):r.profile_path()
        with patch.dict(os.environ, {'MANYING_RPA_PROFILE':str(self.root/'private-profile')}):
            self.assertEqual(r.profile_path(),self.root/'private-profile')


if __name__ == '__main__':
    unittest.main()
