"""Isolated real-UI tests. All non-loopback requests are fulfilled or aborted."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import struct
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
import zlib

import publisherctl as ctl
import rpa_browser as r
import rpa_session as cli
import wechat_rpa as w
import test_publisherctl as publisher_tests


def png20():
    def chunk(kind, payload):
        return struct.pack('>I', len(payload))+kind+payload+struct.pack('>I', zlib.crc32(kind+payload)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR', struct.pack('>IIBBBBB',20,20,8,2,0,0,0))+chunk(b'IDAT',zlib.compress((b'\0'+b'\x80\x90\xa0'*20)*20))+chunk(b'IEND',b'')


EDITOR = '''<!doctype html><meta charset="utf-8"><body>
<a class="weui-desktop-account__nickname">漫映Manying</a><div class="appmsg_account_name">漫映Manying</div>
<div contenteditable="true" class="ProseMirror" data-placeholder="请在这里输入标题">补片指南</div>
<input id="author"><textarea id="js_description"></textarea><input type="file" id="upload">
<div contenteditable="true" class="ProseMirror"></div>
<button id="save" onclick="document.querySelector('#receipt').textContent='手动保存 '+(++window.saves)">保存为草稿</button>
<div id="receipt">旧保存回执</div>
<button class="mass_send" onclick="document.querySelector('#settings').style.display='block'">发表</button>
<div class="weui-desktop-dialog" style="display:none"><button>继续发表</button><input type="checkbox"></div>
<div class="weui-desktop-dialog" id="settings" style="display:none">
 <label class="weui-desktop-form__label">群发通知</label><label class="weui-desktop-switch"><input id="notify" type="checkbox" checked onchange="document.querySelector('#group').style.display=this.checked?'block':'none'"></label>
 <div id="group"><label class="weui-desktop-form__label">分组通知</label><label class="weui-desktop-switch"><input type="checkbox"></label></div>
 <label class="weui-desktop-form__label">定时发表</label><label class="weui-desktop-switch"><input type="checkbox"></label>
 <button onclick="document.querySelector('#scope').textContent=document.querySelector('#notify').checked?'已开启群发通知':'未开启群发通知';document.querySelector('#settings').style.display='none';document.querySelector('#confirm').style.display='block'">发表</button>
</div>
<div class="weui-desktop-dialog" id="confirm" style="display:none"><p id="scope"></p>
 <button onclick="window.submissions++;document.querySelector('#confirm').style.display='none'">继续发表</button></div>
<script>window.submissions=0;window.saves=0;
document.querySelector('#upload').onchange=()=>{setTimeout(()=>{const image=document.createElement('img');image.src='https://mmbiz.qpic.cn/test/upload/640?wx_fmt=png';image.setAttribute('data-imgfileid','fixture-image');const body=document.querySelector('.ProseMirror:not([data-placeholder])');body.querySelector('.editor_content_placeholder')?.remove();body.appendChild(image);const paragraph=document.createElement('p');paragraph.appendChild(document.createElement('br'));body.appendChild(paragraph);},100)};
</script></body>'''

READING = '''<!doctype html><meta charset="utf-8"><body>
<h1 id="activity-name">补片指南</h1><p>原创 漫映Manying 2026年10月13日 23:13</p>
<div id="js_content"><p>第一段&lt;&amp;&gt;不能注入HTML。</p><div style="height:900px"></div>
<img id="lazy" data-src="https://mmbiz.qpic.cn/test/640?wx_fmt=png" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==">
<h2>小标题</h2><p>第二段</p></div><a id="js_view_source" href="https://manying.site/">阅读原文</a>
<script>setTimeout(()=>{const old=document.querySelector('#lazy'),fresh=old.cloneNode();fresh.src=fresh.getAttribute('data-src');old.replaceWith(fresh)},300);</script>
</body>'''


class SessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                data = EDITOR.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type','text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(data)
            def log_message(self,*args):
                pass
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        cls.origin='http://127.0.0.1:'+str(cls.server.server_address[1])
        cls.worker=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join(3)

    def setUp(self):
        self.fixture=publisher_tests.PublisherTests()
        self.fixture.setUp()
        self.fixture.prepare(native=True)
        self.directory=self.fixture.directory

    def tearDown(self):
        self.fixture.tearDown()

    def original_dialog(self, session, opened=False, changed_agreement=False):
        text='我已阅读并同意《微信公众平台原创声明及相关功能使用协议》'
        if changed_agreement:text='新增法律协议，须用户确认'
        html='''<a class="js_unset_original_title" onclick="document.querySelector('#original-dialog').style.display='block'">未声明</a>
        <div id="original-dialog" class="weui-desktop-dialog" style="display:''' + ('block' if opened else 'none') + '''">
        <label><input type="radio" data-label="文字原创">文字原创</label>
        <label><input id="original-check" class="weui-desktop-form__checkbox" type="checkbox" style="display:none" onchange="window.originalChecks=(window.originalChecks||0)+1;document.querySelector('#original-ok').disabled=!this.checked">
        <i class="weui-desktop-icon-checkbox" style="display:inline-block;width:20px;height:20px;background:#ccc"></i><span>''' + text + '''</span></label>
        <button id="original-ok" disabled onclick="document.querySelector('#original-dialog').style.display='none';document.querySelector('.js_unset_original_title').style.display='none'">确定</button></div>'''
        session.page.locator('body').evaluate('(e,html)=>e.insertAdjacentHTML("beforeend",html)',html)

    def test_original_uses_visible_label_icon_and_can_resume_open_dialog(self):
        for opened in [False,True]:
            with self.subTest(opened=opened),self.session() as session:
                self.original_dialog(session,opened=opened)
                result=session.original()
                self.assertEqual(result['state'],'original_selected')
                self.assertTrue(session.page.locator('#original-check').is_checked())
                self.assertEqual(session.page.evaluate('window.originalChecks'),1)
                self.assertFalse(session.page.locator('#original-dialog').is_visible())

    def test_original_waits_for_dialog_and_ignores_hidden_options(self):
        with self.session() as session:
            self.original_dialog(session)
            session.page.locator('#original-dialog').evaluate('''el=>{
                el.insertAdjacentHTML('beforeend', '<label style="display:none"><input id="hidden-original" type="radio" data-label="文字原创">文字原创</label>');
            }''')
            session.page.locator('.js_unset_original_title').evaluate('''el=>{
                el.setAttribute('onclick', "setTimeout(()=>document.querySelector('#original-dialog').style.display='block',250)");
            }''')
            self.assertEqual(session.original()['state'],'original_selected')
            self.assertFalse(session.page.locator('#hidden-original').is_checked())
            self.assertTrue(session.page.locator('#original-check').is_checked())
            self.assertEqual(session.page.evaluate('window.originalChecks'),1)

    def test_original_changed_legal_text_is_never_auto_accepted(self):
        with self.session() as session:
            self.original_dialog(session,opened=True,changed_agreement=True)
            with self.assertRaisesRegex(ValueError,'New legal declaration'):
                session.original()
            self.assertFalse(session.page.locator('#original-check').is_checked())

    @contextmanager
    def session(self, authorized=True):
        with r.browser_session(channel='chromium',temporary=True) as context:
            def route(request):
                url=request.request.url
                if url.startswith(self.origin):
                    request.continue_()
                elif url.startswith('https://mmbiz.qpic.cn/'):
                    request.fulfill(status=200,content_type='image/png',body=png20())
                elif url.startswith('https://mp.weixin.qq.com/s/'):
                    request.fulfill(status=200,content_type='text/html; charset=utf-8',body=READING)
                else:
                    request.abort()
            context.route('**/*',route)
            page=context.new_page()
            page.goto(self.origin)
            session=w.Session(context,self.fixture.root,authorized_draft=authorized,
                              authorized_publish=authorized,origin=self.origin)
            session.page=page
            session.article(str(self.directory))
            yield session

    def test_no_authorization_or_wrong_account_cannot_change_editor(self):
        with self.session(authorized=False) as session:
            with self.assertRaises(ValueError):
                session.prepare()
            self.assertEqual(session.page.locator(w.TITLE).inner_text(),'补片指南')
        with self.session() as session:
            session.page.locator('.appmsg_account_name').evaluate("e=>e.textContent='其他账号'")
            with self.assertRaises(ValueError):
                session.prepare()
            self.assertEqual(session.page.locator('#author').input_value(),'')

    def test_homepage_account_locator_keeps_exact_identity_guard(self):
        with self.session() as session:
            session.page.locator('.weui-desktop-account__nickname').evaluate('e=>e.remove()')
            account = session.page.locator('.appmsg_account_name')
            account.evaluate("e=>e.className='acount_box-nickname'")
            session.guard()
            account = session.page.locator('.acount_box-nickname')
            account.evaluate("e=>e.textContent='其他账号'")
            with self.assertRaisesRegex(ValueError, 'Wrong or unconfirmed'):
                session.guard()
            account.evaluate("e=>e.textContent='漫映Manying'")
            account.evaluate("e=>e.after(e.cloneNode(true))")
            with self.assertRaisesRegex(ValueError, 'Wrong or unconfirmed'):
                session.guard()

    def test_publication_history_navigation_does_not_allow_final_publish(self):
        with self.session() as session:
            session.page.evaluate("""() => {
                const link = document.createElement('a');
                link.id = 'menu_10126'; link.textContent = '发表记录';
                document.body.appendChild(link);
            }""")
            self.assertTrue(session.click('#menu_10126', '发表记录')['action_sent'])
            with self.assertRaisesRegex(ValueError, 'dedicated publication'):
                session.click('button.mass_send', '发表')
            self.assertEqual(session.page.evaluate('window.submissions'), 0)

    def test_prepare_uploads_native_image_and_waits_for_real_paste(self):
        with self.session() as session:
            session.page.locator(w.TITLE).fill('')
            session.page.locator(w.BODY).evaluate("e=>e.innerHTML='<div class=editor_content_placeholder contenteditable=false style=\"pointer-events:none;user-select:none\">从这里开始写正文</div>'")
            result=session.prepare()
            self.assertEqual(result['state'],'body_prepared')
            self.assertIn('第一段<&>不能注入HTML。',session.page.locator(w.BODY).inner_text())
            self.assertEqual(session.page.locator(w.BODY+' img[data-imgfileid]').count(),1)
            self.assertIn('poster.png',ctl.read_json(self.directory/'native-images.json'))
            self.assertEqual(ctl.load_manifest(self.directory)['checks'],{})
            with self.assertRaises(ValueError):
                session.prepare()

    def test_fresh_save_required_and_does_not_mark_visual_checks(self):
        with self.session() as session:
            result=session.save('#receipt','手动保存')
            self.assertEqual(result['state'],'editor_saved')
            self.assertEqual(ctl.load_manifest(self.directory)['checks'],{})
            session.page.locator('#save').evaluate('e=>e.onclick=()=>{}')
            # Simulate a lost acknowledgement after a real click, without a 15 s wait.
            with patch.object(session.page, 'wait_for_function', side_effect=RuntimeError('no new receipt')):
                with self.assertRaises(RuntimeError):
                    session.save('#receipt','手动保存')
            self.assertEqual(session.prior()['status'],'uncertain')

    def test_save_does_not_accept_a_temporarily_empty_receipt(self):
        with self.session() as session:
            session.page.locator('#save').evaluate('''e=>e.onclick=()=>{
              const receipt=document.querySelector('#receipt');receipt.textContent='';
              setTimeout(()=>receipt.textContent='新的手动保存回执',150);
            }''')
            result=session.save('#receipt','手动保存')
            self.assertEqual(result['state'],'editor_saved')
            self.assertEqual(result['receipt'],'新的手动保存回执')

    def test_settings_review_and_single_submit_with_hidden_old_dialog(self):
        self.fixture.mark_saved()
        with self.session() as session:
            opened=session.publication_open()
            self.assertTrue(opened['settings']['群发通知'])
            self.assertEqual(session.page.evaluate('window.submissions'),0)
            configured=session.publication_config(notify=False)
            self.assertEqual(configured['settings'],{'群发通知':False,'定时发表':False})
            review=session.publication_review()
            self.assertFalse(review['submit_sent'])
            self.assertEqual(session.page.evaluate('window.submissions'),0)
            self.assertEqual(session.submit()['state'],'submitted')
            self.assertEqual(session.page.evaluate('window.submissions'),1)
            with self.assertRaises(ValueError):
                session.submit()
            self.assertEqual(session.page.evaluate('window.submissions'),1)
            self.assertEqual(ctl.load_manifest(self.directory)['status'],'submitted')

    def test_submit_intent_blocks_replay_after_restart_and_notify_needs_scope(self):
        self.fixture.mark_saved()
        with self.session() as session:
            session.publication_open()
            with self.assertRaises(ValueError):
                session.publication_config(notify=True)
            session.checkpoint('submit','pending')
            session.checkpoint('click','completed')
            resumed=w.Session(session.context,self.fixture.root,authorized_publish=True,origin=self.origin)
            resumed.page=session.page
            resumed.article(str(self.directory))
            with self.assertRaises(ValueError):
                resumed.publication_open()
            self.assertEqual(session.page.evaluate('window.submissions'),0)

    def test_server_batch_rejects_a_fourth_directory_and_historical_article(self):
        self.fixture.mark_saved()
        job_file = self.fixture.root / 'job.json'
        with self.session() as session:
            ctl.write_json(job_file, {'count':3,'state':'running','before':[]})
            with patch.dict(w.os.environ, {'MANYING_BATCH_JOB':str(job_file)}):
                session.publication_guard()
                for i in range(3):
                    extra=self.fixture.root/('extra-'+str(i));extra.mkdir()
                    ctl.write_json(extra/'manifest.json', {'status':'draft'})
                with self.assertRaisesRegex(ValueError, 'exceeds three'):
                    session.publication_guard()
                ctl.write_json(job_file, {'count':3,'state':'running','before':[self.directory.name]})
                with self.assertRaisesRegex(ValueError, 'outside this server batch'):
                    session.publication_guard()
                self.assertEqual(session.page.evaluate('window.submissions'),0)

    def test_lost_final_acknowledgement_is_unknown_and_never_replayed(self):
        self.fixture.mark_saved()
        with self.session() as session:
            session.publication_open()
            session.publication_config(notify=False)
            session.publication_review()
            from playwright.sync_api import Locator
            original=Locator.click
            def lost(locator,*args,**kwargs):
                original(locator,*args,**kwargs)
                raise RuntimeError('fixture acknowledgement lost')
            with patch.object(Locator,'click',lost):
                with self.assertRaises(RuntimeError):
                    session.submit()
            self.assertEqual(ctl.load_manifest(self.directory)['status'],'publication_unknown')
            self.assertEqual(session.page.evaluate('window.submissions'),1)
            session.checkpoint('click','completed')
            with self.assertRaises(ValueError):
                session.submit()
            self.assertEqual(session.page.evaluate('window.submissions'),1)

    def test_queue_history_cannot_be_used_as_published_or_expose_tempkey(self):
        with self.session() as session:
            session.page.locator('body').evaluate('''e=>e.insertAdjacentHTML('beforeend',
             '<section>2026-10-14 09:00<p>已发表</p><a class="weui-desktop-mass-appmsg__title" href="https://mp.weixin.qq.com/s/other"><span>其他文章</span></a></section><section><p>审核中</p><a class="weui-desktop-mass-appmsg__title" href="https://mp.weixin.qq.com/s?tempkey=do-not-emit"><span>补片指南</span></a></section>')''')
            result=session.history()
            self.assertEqual(result['matches'][0]['status'],'审核中')
            self.assertIsNone(result['matches'][0]['public_url'])
            self.assertNotIn('do-not-emit',json.dumps(result))
            with self.assertRaises(ValueError):
                session.open_public()

    def test_public_readcheck_requeries_replaced_image_and_reconciles_without_fake_submit(self):
        with self.session() as session:
            mapping = ctl.read_json(self.directory/'native-images.json')
            mapping['poster.png'] = mapping['poster.png'].replace('/640?', '/0?')
            ctl.write_json(self.directory/'native-images.json', mapping)
            ctl.render(NS(dir=str(self.directory)))
            session.page.locator('body').evaluate('''e=>e.insertAdjacentHTML('beforeend',
             '<section>2026-10-13 23:12<p>已发表</p><a target="_blank" class="weui-desktop-mass-appmsg__title" href="https://mp.weixin.qq.com/s/offline"><span>补片指南</span></a></section>')''')
            session.open_public()
            session.origin='https://mp.weixin.qq.com'
            result=session.readcheck()
            self.assertTrue(result['images'][0]['loaded'])
            self.assertTrue((self.directory/'wechat-public.png').is_file())
            session.record_published('Synthetic backend and public-page verification; no real publication')
            manifest=ctl.load_manifest(self.directory)
            self.assertEqual(manifest['status'],'published')
            self.assertEqual(manifest['published_at'],'2026-10-13T23:13:00+08:00')
            self.assertEqual(manifest['history'][-1]['event'],'published-reconciled')
            self.assertNotIn('submit-requested',[x['event'] for x in manifest['history']])
            import server_runner
            self.assertTrue(server_runner.proof_for(self.directory,manifest))

    def test_image_identity_allows_cdn_size_variant_but_not_another_image(self):
        original = 'https://mmbiz.qpic.cn/mmbiz_jpg/same-image/0?wx_fmt=jpeg'
        reading = 'https://mmbiz.qpic.cn/mmbiz_jpg/same-image/640?wx_fmt=jpeg'
        self.assertEqual(w.image_identity(original), w.image_identity(reading))
        self.assertNotEqual(w.image_identity(original),
                            w.image_identity(reading.replace('same-image', 'other-image')))
        with self.assertRaisesRegex(ValueError, 'native WeChat'):
            w.image_identity(reading.replace('mmbiz.qpic.cn', 'example.com'))

    def test_resume_preview_cannot_adopt_an_unsaved_article(self):
        with self.session() as session:
            with self.assertRaisesRegex(ValueError, 'record a fresh save'):
                session.open_preview(0, resume_saved=True)

    def test_reencoded_image_keeps_file_id_and_rejects_other_upload(self):
        native = '<img src="https://mmbiz.qpic.cn/mmbiz_jpg/original/0" data-imgfileid="123">'
        reading = {'src':'https://mmbiz.qpic.cn/mmbiz_jpg/reencoded/640', 'file_id':'123'}
        self.assertTrue(w.same_native_image(native, reading))
        self.assertFalse(w.same_native_image(native, {**reading, 'file_id':'124'}))
        with self.assertRaisesRegex(ValueError, 'native WeChat'):
            w.same_native_image(native, {**reading, 'src':'https://example.com/reencoded/640'})

    def test_reload_preserves_context_but_is_blocked_after_submission(self):
        with self.session() as session:
            context = session.context
            self.assertTrue(cli.dispatch(session, {'op':'reload-local-code'})['same_browser_context'])
            self.assertIs(session.context, context)
            with patch.object(session, 'manifest', return_value={'status':'submitted'}):
                with self.assertRaisesRegex(ValueError, 'in-flight publication'):
                    cli.dispatch(session, {'op':'reload-local-code'})

    def test_json_transport_redacts_urls_and_rejects_arbitrary_code(self):
        with self.session() as session:
            args=NS(authorized_draft=False,authorized_publish=False,authorized_notify=False,headed=False)
            output=io.StringIO()
            requests='{"op":"evaluate","code":"send()"}\n{"op":"quit"}\n'
            cli.serve(session.context,self.fixture.root,args,input_stream=io.StringIO(requests),output_stream=output,navigate=False)
            rows=[json.loads(line) for line in output.getvalue().splitlines()]
            self.assertFalse(rows[1]['ok'])
            self.assertTrue(rows[2]['result']['closed'])
            clean=w.redact({'url':'https://mp.weixin.qq.com/s?tempkey=do-not-emit','public':'https://mp.weixin.qq.com/s/offline'})
            self.assertNotIn('do-not-emit',json.dumps(clean))
            self.assertEqual(clean['public'],'https://mp.weixin.qq.com/s/offline')


if __name__=='__main__':
    unittest.main()
