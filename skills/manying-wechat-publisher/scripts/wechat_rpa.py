"""Normal WeChat UI operations for one local Playwright session. No private APIs."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import hashlib
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace as NS
from urllib.parse import urlsplit

import archive_articles as archive
import publisherctl as ctl
import rpa_browser as r

TITLE = '.ProseMirror[data-placeholder]'
BODY = '.ProseMirror:not([data-placeholder])'
DIALOG = '.weui-desktop-dialog:visible'
READ_IMAGES = '#js_content img[data-src]'
INFLIGHT = {'submitted', 'awaiting_admin', 'publication_unknown'}
READ_NAV = {'首页', '内容管理', '草稿箱', '发表记录', '全部发表记录', '查看全部', '下一页', '上一页'}
STATUS_RE = re.compile(r'^(已发表|审核中|等候发表|待发表|发表失败|发表中|定时发表)$', re.M)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def visible_dialog(page):
    return r.one(page, DIALOG)


def safe_url(value):
    try:
        return ctl.public_url(value or '', published=True)
    except ValueError:
        return None


def image_identity(value):
    parsed = urlsplit(value)
    require(parsed.scheme == 'https' and parsed.hostname == 'mmbiz.qpic.cn',
            'Reading image is not a native WeChat CDN image')
    return re.sub(r'/(?:0|300|640|800|1024|1080)$', '', parsed.path)


def redact(value):
    """Keep verified public article URLs; never emit authenticated/temporary URLs."""
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return re.sub(r'https?://[^\s<>"\']+',
                      lambda m: safe_url(m[0]) or r.sanitized(m[0]), value)
    return value


def history_rows(page):
    """Read the nearest visible publication group, not a sibling group's status."""
    rows = page.locator('a.weui-desktop-mass-appmsg__title:visible').evaluate_all("""els => els.map(e => {
        let parent = e.parentElement, group = '';
        while (parent && parent.tagName !== 'BODY') {
            const text = parent.innerText || '';
            if (/^(已发表|审核中|等候发表|待发表|发表失败|发表中|定时发表)$/m.test(text)) { group=text; break; }
            parent=parent.parentElement;
        }
        return {title:(e.querySelector('span')?.innerText || e.innerText).trim(),
                href:e.getAttribute('href'), group:group.slice(0,1800)};
    })""")
    result = []
    for row in rows:
        match = STATUS_RE.search(row['group'])
        result.append({'title': row['title'], 'status': match[0] if match else 'unknown',
                       'group_text': row['group'], 'public_url': safe_url(row['href'])})
    return result


class ImageSrc(HTMLParser):
    def __init__(self):
        super().__init__()
        self.src = ''
        self.file_id = ''

    def handle_starttag(self, tag, attrs):
        if tag == 'img':
            self.src = dict(attrs).get('src', '')
            self.file_id = dict(attrs).get('data-imgfileid', '')


def same_native_image(native, reading):
    parser = ImageSrc()
    parser.feed(ctl.native_html(native))
    original_key, reading_key = image_identity(parser.src), image_identity(reading['src'])
    observed_id = reading.get('file_id', '')
    if parser.file_id and observed_id:
        # Saving can re-encode a CDN object under a new URL. The upload's native
        # file ID survives in the saved reading DOM and is its stable identity.
        return bool(re.fullmatch(r'[0-9]+', parser.file_id)) and parser.file_id == observed_id
    return original_key == reading_key


class Session:
    def __init__(self, context, root, *, authorized_draft=False, authorized_publish=False,
                 authorized_notify=False, origin=r.HOME.rstrip('/')):
        self.context, self.root, self.origin = context, Path(root).resolve(), origin
        self.page = context.pages[0] if context.pages else context.new_page()
        self.directory = None
        self.authorized_draft, self.authorized_publish = authorized_draft, authorized_publish
        self.authorized_notify = authorized_notify
        self.last_shot = None
        self.saved = None
        self.preview = None
        self.public_proof = None
        self.history_proof = None
        self.history_checked = False
        self.config = None

    def article(self, directory):
        directory = (self.root / directory).resolve()
        require(directory != self.root and directory.is_relative_to(self.root), 'Article must stay under --root')
        manifest = ctl.load_manifest(directory)
        self.directory = directory
        self.saved = self.preview = self.public_proof = self.history_proof = self.config = None
        self.history_checked = False
        return {'title': manifest['title'], 'status': manifest['status'], 'dir': str(directory)}

    def manifest(self):
        require(self.directory is not None, 'Select the existing article directory first')
        return ctl.load_manifest(self.directory)

    def guard(self, editor=False, write=False):
        r.checked_page(self.page, self.origin)
        manifest = self.manifest()
        account = self.page.locator('.appmsg_account_name:visible')
        if not account.count():
            account = self.page.locator('.weui-desktop-account__nickname:visible')
        if not account.count():
            account = self.page.locator('.acount_box-nickname:visible')
        require(account.count() == 1 and r.norm(account.inner_text()) == r.norm(manifest['account']),
                'Wrong or unconfirmed WeChat account')
        if write:
            require(self.authorized_draft, 'Current draft authorization is required')
            ctl.load_manifest(self.directory, mutable=True)
            ctl.require_editable_content(manifest)
        if editor:
            require(r.norm(r.control_text(r.one(self.page, TITLE))) == r.norm(manifest['title']),
                    'Another article is open; no action sent')
        return manifest

    def checkpoint(self, op, status, **extra):
        self.manifest()
        with ctl.locked(self.directory):
            ctl.write_json(self.directory / 'rpa-session-state.json',
                           {'op': op, 'status': status, 'at': ctl.now(), **extra})
            if op == 'submit':
                # Read-only navigation must never erase an unacknowledged submit intent.
                ctl.write_json(self.directory / 'rpa-submit-intent.json',
                               {'op': op, 'status': status, 'at': ctl.now(), **extra})

    def prior(self):
        path = self.directory / 'rpa-session-state.json'
        return ctl.read_json(path) if path.exists() else {}

    def mutate(self, op, action):
        self.last_shot = None
        self.checkpoint(op, 'pending')
        try:
            value = action()
        except Exception:
            self.checkpoint(op, 'uncertain')
            raise
        self.checkpoint(op, 'completed')
        return value

    def invalidate_editor_checks(self, *fields):
        with ctl.locked(self.directory):
            manifest = ctl.load_manifest(self.directory, mutable=True)
            ctl.require_editable_content(manifest)
            manifest['status'] = 'prepared'
            for field in fields:
                manifest.get('checks', {}).pop(field, None)
            ctl.save_event(self.directory, manifest, 'editor-observation-invalidated',
                           '本地RPA改变或重新保存当前编辑版本，须重新核验：'+', '.join(fields))
        self.saved = self.preview = self.public_proof = None

    def observe(self, selector='body', limit=3000):
        # This is also usable on a login page. No authentication fields/URLs are read.
        node = r.one(self.page, selector)
        limit = max(100, min(int(limit), 6000))
        return redact({'path': r.sanitized(self.page.url), 'title': self.page.title(),
                       'text': node.inner_text()[:limit],
                       'controls': node.locator('a:visible,button:visible,input:not([type=hidden]):not([type=password]):visible,textarea:visible,[contenteditable=true]:visible').evaluate_all("""els => els.slice(0,60).map(e=>({tag:e.tagName,id:e.id,
                         cls:typeof e.className==='string'?e.className:'',text:(e.innerText||e.getAttribute('aria-label')||'').slice(0,100),
                         type:e.getAttribute('type'),placeholder:e.getAttribute('placeholder')}))""")})

    def dom(self, selector):
        # Metadata only: outerHTML of a visible parent may contain hidden credentials.
        return redact(self.page.locator(selector).evaluate_all("""els=>els.slice(0,25).map(e=>{
          const visible=Boolean(e.getClientRects().length);
          if(!visible)return {visible:false};
          const meta=x=>({tag:x.tagName,id:x.id,cls:typeof x.className==='string'?x.className:''});
          let imagePath=null;
          if(e.tagName==='IMG')try{const u=new URL(e.getAttribute('data-src')||e.getAttribute('src'));
            if(u.protocol==='https:'&&u.hostname==='mmbiz.qpic.cn')imagePath=u.pathname;}catch{}
          return {...meta(e),visible,text:(e.innerText||'').slice(0,300),
            image_path:imagePath,
            attributes:Object.fromEntries(['contenteditable','data-placeholder','data-imgfileid','type','name','role','placeholder','readonly'].map(k=>[k,e.getAttribute(k)])),
            parent:e.parentElement?meta(e.parentElement):null,children:Array.from(e.children).slice(0,12).map(meta)};
        })"""))

    def tabs(self):
        return [{'tab': i, 'title': p.title(), 'path': r.sanitized(p.url)}
                for i, p in enumerate(self.context.pages) if not p.is_closed()]

    def use(self, tab):
        self.page = self.context.pages[int(tab)]
        self.last_shot = None
        return self.observe(limit=600)

    def click_control(self, selector, text):
        r.checked_page(self.page, self.origin)
        node = r.one(self.page, selector)
        require(r.label(node) == r.norm(text), 'Button text changed; no click sent')
        node.click(timeout=10000)

    def click(self, selector, text, next_selector=None, new_page=False, key=False):
        require(r.norm(text) in READ_NAV or not r.FORBIDDEN.search(text),
                'Use dedicated publication/original operations; authentication is manual')
        self.guard(write=r.norm(text) not in READ_NAV)
        if r.norm(text) in {'新的创作', '文章'}:
            require(self.history_checked and not self.history_proof,
                    'Inspect publication history for this article before creating a draft')
            matches = self.page.locator('a.weui-desktop-publish__cover__title:visible').filter(has_text=self.manifest()['title'])
            require(not matches.count(), 'An existing draft matches; recover its edit entry instead of creating another')
        node = r.one(self.page, selector)
        require(r.label(node) == r.norm(text), 'Control changed; no action sent')
        def action():
            if new_page:
                with self.context.expect_page(timeout=15000) as opened:
                    node.press('Enter') if key else node.click(timeout=10000)
                self.page = opened.value
                self.page.wait_for_load_state('domcontentloaded', timeout=20000)
            else:
                node.press('Enter') if key else node.click(timeout=10000)
            if next_selector:
                r.one(self.page, next_selector).wait_for(timeout=15000)
            return {'action_sent': True, 'observe_next': True}
        return self.mutate('click', action)

    def hover(self, selector):
        self.guard()
        r.one(self.page, selector).hover()
        return {'hovered': True}

    def screenshot(self, name='rpa-current.png', full_page=False):
        r.checked_page(self.page, self.origin)
        path = ctl.local_file(self.directory, name)
        require(path.suffix.lower() == '.png', 'Screenshot must be a PNG inside this article')
        self.page.screenshot(path=str(path), full_page=bool(full_page))
        self.last_shot = {'path': str(path), 'page': self.page, 'url': self.page.url}
        return {'screenshot': str(path)}

    def pointer(self, kind, x, y, end_x=None, end_y=None):
        self.guard(editor=True, write=True)
        require(self.last_shot and self.last_shot['page'] is self.page and
                self.last_shot['url'] == self.page.url, 'Inspect a fresh screenshot before a crop coordinate action')
        require('编辑封面' in visible_dialog(self.page).inner_text(), 'Coordinates are only for the current cover crop dialog')
        require(kind in {'click', 'drag'}, 'Only crop click/drag is supported')
        bounds = visible_dialog(self.page).bounding_box()
        def inside(px, py):
            return bounds and bounds['x'] <= px <= bounds['x']+bounds['width'] and bounds['y'] <= py <= bounds['y']+bounds['height']
        require(inside(x, y) and (kind != 'drag' or inside(end_x, end_y)), 'Crop coordinates must stay inside the current dialog')
        def action():
            if kind == 'click':
                self.page.mouse.click(x, y)
            else:
                self.page.mouse.move(x, y)
                self.page.mouse.down()
                try:
                    self.page.mouse.move(end_x, end_y, steps=12)
                finally:
                    self.page.mouse.up()
            return {'crop_action_sent': True, 'inspect_new_screenshot': True}
        return self.mutate('crop', action)

    def prepare(self):
        manifest = self.guard(write=True)
        require(ctl.check(self.directory)['ok'], 'Local preparation checks failed')
        require(self.prior().get('status') not in {'pending', 'uncertain'} or self.prior().get('op') != 'prepare',
                'Previous preparation outcome is unknown; inspect the existing editor instead of replaying')
        for path in (self.directory / 'rpa-state.json',):
            old = ctl.read_json(path) if path.exists() else {}
            require(old.get('state') not in {'pending', 'running', 'uncertain'}, 'Legacy RPA has an unknown action; inspect it first')
        require(not r.control_text(r.one(self.page, TITLE)).strip(), 'Only a confirmed empty editor may be initialized')
        editor = r.one(self.page, BODY)
        require(editor.inner_text().strip() in {'', '从这里开始写正文'} and
                not editor.locator('img:not(.ProseMirror-separator),video,iframe').count(), 'Body is not empty')
        content = ctl.load_content(self.directory)
        files = list(dict.fromkeys(b['file'] for b in content['blocks'] if b['type'] == 'image'))
        registered = {x['file'] for x in manifest['images']}
        require(set(files) <= registered, 'Only registered article images may be uploaded')
        def action():
            self.page.locator(TITLE).fill(manifest['title'])
            self.page.locator('#author').fill(manifest.get('author', manifest['account']))
            self.page.locator('#js_description').fill(content['summary'])
            mapping = {}
            for name in files:
                before = set(self.page.locator(BODY+' img[data-imgfileid]').evaluate_all('els=>els.map(e=>e.getAttribute("data-imgfileid"))'))
                self.checkpoint('prepare', 'pending', step='upload', file=name)
                self.page.locator('input[type=file]').set_input_files(str(ctl.local_file(self.directory, name)))
                self.page.wait_for_function("""before=>Array.from(document.querySelectorAll('.ProseMirror:not([data-placeholder]) img[data-imgfileid]')).filter(e=>!before.includes(e.getAttribute('data-imgfileid'))&&e.complete&&e.naturalWidth>10).length===1""", arg=list(before), timeout=30000)
                rows = self.page.locator(BODY+' img[data-imgfileid]').evaluate_all('els=>els.map(e=>({id:e.getAttribute("data-imgfileid"),html:e.outerHTML}))')
                added = [x for x in rows if x['id'] not in before]
                require(len(added) == 1, 'Upload mapping is ambiguous')
                ctl.native_html(added[0]['html'])
                mapping[name] = added[0]['html']
            ctl.write_json(self.directory/'native-images.json', mapping)
            ctl.render(NS(dir=str(self.directory)))
            plan = {'schema_version': 1, 'account': {'selector': '.appmsg_account_name', 'text': manifest['account']},
                    'title_selector': TITLE, 'editor_selector': BODY,
                    'steps': [{'action': 'paste-html', 'selector': BODY}]}
            proof = next(b['text'][:60] for b in content['blocks'] if b['type'] == 'p')
            values = {'title': manifest['title'], 'html': (self.directory/'paste.html').read_text(encoding='utf-8'), 'body_proof': proof}
            r.execute_steps(self.page, plan, values, self.directory, origin=self.origin)
            require(self.page.locator(BODY+' img[data-imgfileid]').count() == len(files), 'Pasted image count differs')
            return {'title': manifest['title'], 'body_images': files, 'state': 'body_prepared'}
        return self.mutate('prepare', action)

    def cover_select(self, index):
        self.guard(editor=True, write=True)
        def action():
            self.click_control('.js_share_type_none_image', '拖拽或选择封面')
            self.page.locator(':text-is("从正文选择"):visible').wait_for()
            self.click_control(':text-is("从正文选择"):visible', '从正文选择')
            items = self.page.locator('.appmsg_content_img_item')
            require(0 <= int(index) < items.count(), 'Cover image index is unavailable')
            items.nth(int(index)).click()
            self.click_control(DIALOG+' button:text-is("下一步")', '下一步')
            self.page.locator(DIALOG+' button:text-is("确认")').wait_for(timeout=20000)
            return {'state': 'crop_ready', 'needs_visual_review': True}
        return self.mutate('cover-select', action)

    def cover_confirm(self):
        self.guard(editor=True, write=True)
        require(self.last_shot and '编辑封面' in visible_dialog(self.page).inner_text(), 'Inspect both cover crops first')
        self.invalidate_editor_checks('cover', 'preview')
        return self.mutate('cover-confirm', lambda: self.click_control(DIALOG+' button:text-is("确认")', '确认'))

    def original(self):
        self.guard(editor=True, write=True)
        self.invalidate_editor_checks('creation-source', 'preview')
        def action():
            # Recover the same already-open dialog after an interrupted checkbox action.
            if not self.page.locator(DIALOG).count():
                self.click_control('.js_unset_original_title', '未声明')
            option_selector=DIALOG+' label:has(input[data-label="文字原创"]):visible'
            self.page.locator(option_selector).wait_for(state='visible', timeout=10000)
            option=r.one(self.page,option_selector)
            if not option.locator('input').is_checked():option.click()
            agreement_selector=DIALOG+' label:has(input.weui-desktop-form__checkbox):visible'
            self.page.locator(agreement_selector).wait_for(state='visible', timeout=10000)
            agreement = r.one(self.page,agreement_selector)
            require(r.label(agreement) ==
                    '我已阅读并同意《微信公众平台原创声明及相关功能使用协议》', 'New legal declaration requires user handling')
            checkbox=agreement.locator('input.weui-desktop-form__checkbox')
            require(checkbox.count()==1 and checkbox.get_attribute('type')=='checkbox','Original agreement control changed')
            if not checkbox.is_checked():
                # The real WeChat control hides its native input; its visible icon belongs to the label.
                icon=agreement.locator('i.weui-desktop-icon-checkbox:visible')
                require(icon.count()==1,'Inspect the visible original agreement checkbox icon')
                icon.click()
            from playwright.sync_api import expect
            expect(checkbox).to_be_checked(timeout=5000)
            self.click_control(DIALOG+' button:text-is("确定")', '确定')
            self.page.locator('.js_unset_original_title').wait_for(state='hidden')
            # Hidden reward controls can be checked while inapplicable; inspect the visible setting instead.
            return {'state': 'original_selected', 'inspect_author_and_reward': True}
        return self.mutate('original', action)

    def links_source(self, source):
        self.guard(editor=True, write=True)
        require(source in {'内容由AI生成', '素材来源官方媒体/网络新闻', '内容剧情演绎，仅供娱乐', '个人观点，仅供参考', '无需声明'}, 'Use an actually applicable source option')
        content = ctl.load_content(self.directory)
        url = ctl.public_url(content.get('website_url', 'https://manying.site/'))
        self.invalidate_editor_checks('creation-source', 'preview')
        def action():
            if url not in self.page.locator('.js_article_url_allow_click').inner_text():
                self.click_control('.js_article_url_allow_click', '未添加 icon')
                self.page.locator('input.js_url:visible').fill(url)
                self.click_control('a.jsPopoverBt:text-is("确定"):visible', '确定')
                self.page.locator('input.js_url:visible').wait_for(state='hidden')
            if source not in self.page.locator('.js_claim_source_desc').inner_text():
                self.click_control('.js_claim_source_desc', '未添加 icon')
                option = self.page.locator(DIALOG+' label').filter(has=self.page.locator('span:text-is('+json.dumps(source, ensure_ascii=False)+')'))
                require(option.count() == 1, 'Source option changed; inspect current DOM')
                option.click()
                self.click_control(DIALOG+' button:text-is("确认")', '确认')
                self.page.locator(DIALOG).wait_for(state='hidden')
            return {'source': self.page.locator('.js_claim_source_desc').inner_text(), 'url': url}
        return self.mutate('links-source', action)

    def save(self, receipt_selector, receipt_text, selector='button:has-text("保存为草稿"):visible', text='保存为草稿'):
        manifest = self.guard(editor=True, write=True)
        require(text in {'保存', '保存草稿', '保存为草稿'}, 'Only a draft-save control is allowed')
        require(ctl.check(self.directory)['ok'], 'Local preparation checks failed')
        receipt = self.page.locator(receipt_selector)
        require(receipt.count() <= 1, 'Receipt must be one observed history/receipt container')
        before = receipt.inner_text() if receipt.count() and receipt.is_visible() else ''
        self.invalidate_editor_checks('preview')
        def action():
            self.click_control(selector, text)
            from playwright.sync_api import expect
            expect(receipt).to_be_visible(timeout=15000)
            self.page.wait_for_function("""data=>{
              const e=document.querySelector(data.selector);
              if(!e||!e.getClientRects().length)return false;
              const value=e.innerText;
              return value!==data.before&&value.includes(data.text);
            }""", arg={'selector': receipt_selector, 'before': before, 'text': receipt_text}, timeout=15000)
            observed = receipt.inner_text()
            require(observed != before and receipt_text in observed, 'No fresh matching save receipt')
            ctl.record(NS(dir=str(self.directory), event='draft-saved', note=ctl.safe_note('本地RPA观察到新保存回执：'+r.sanitized(observed)), url=None))
            self.saved = {'page': self.page, 'digest': manifest.get('render_digest'), 'receipt': r.sanitized(observed)}
            return {'state': 'editor_saved', 'receipt': self.saved['receipt']}
        return self.mutate('save', action)

    def open_preview(self, tab, resume_saved=False):
        manifest = self.manifest()
        require(isinstance(resume_saved, bool), 'resume_saved must be a boolean')
        require(manifest['status'] == 'editor_saved' and (self.saved or resume_saved),
                'Observe and record a fresh save before preview')
        digest = self.saved['digest'] if self.saved else manifest.get('render_digest')
        home = self.context.pages[int(tab)]
        home.reload(wait_until='domcontentloaded', timeout=20000)
        self.page = home
        self.guard()
        card = home.locator('a.weui-desktop-publish__cover__title').filter(has_text=self.manifest()['title'])
        card.wait_for(timeout=15000)
        require(card.count() == 1, 'Saved article card is ambiguous')
        with self.context.expect_page(timeout=15000) as opened:
            card.click()
        self.page = opened.value
        self.page.wait_for_load_state('domcontentloaded', timeout=20000)
        self.page.locator('#js_content').wait_for(timeout=30000)
        self.preview = {'page': self.page, 'digest': digest}
        return {'state': 'current_saved_preview_open', 'observe_next': True}

    def readcheck(self):
        manifest, content = self.manifest(), ctl.load_content(self.directory)
        r.checked_page(self.page, self.origin)
        require(r.norm(self.page.locator('#activity-name').inner_text()) == r.norm(manifest['title']), 'Reading title differs')
        actual = r.norm(self.page.locator('#js_content').inner_text())
        cursor = 0
        for block in content['blocks']:
            if block['type'] in {'p', 'h2'}:
                text = r.norm(block['text'])
                found = actual.find(text, cursor)
                require(found >= 0, 'Saved/public paragraphs differ or are out of order')
                cursor = found + len(text)
        images = self.page.locator(READ_IMAGES)
        files = [b['file'] for b in content['blocks'] if b['type'] == 'image']
        require(images.count() == len(files), 'Reading image count differs')
        for i in range(images.count()):
            images.nth(i).scroll_into_view_if_needed()
            # WeChat replaces lazy placeholders. Re-query the live node on every poll.
            self.page.wait_for_function("""i=>{const e=document.querySelectorAll('#js_content img[data-src]')[i];return e&&e.complete&&e.naturalWidth>10}""", arg=i, timeout=20000)
        rows = images.evaluate_all('els=>els.map(e=>({src:e.getAttribute("data-src"),file_id:e.getAttribute("data-imgfileid")||"",loaded:e.complete&&e.naturalWidth>10,width:e.naturalWidth,height:e.naturalHeight}))')
        mapping_path = self.directory/'native-images.json'
        if mapping_path.exists():
            mapping = ctl.read_json(mapping_path)
            for name, row in zip(files, rows):
                require(same_native_image(mapping[name], row),
                        'Reading image order/identity differs: ' + name)
        require(self.page.locator('#js_view_source').count() == 1, 'Reading-original entrance is missing')
        url = safe_url(self.page.url)
        if not url:
            require(self.preview and self.preview['page'] is self.page and self.preview['digest'] == manifest.get('render_digest'), 'Old/unbound temporary preview is not accepted')
        self.page.keyboard.press('Control+Home')
        self.page.wait_for_function('()=>window.scrollY===0', timeout=5000)
        name = 'wechat-public.png' if url else 'wechat-preview.png'
        self.page.screenshot(path=str(self.directory/name), full_page=True)
        body = self.page.locator('body').inner_text()
        proof = {'digest': manifest.get('render_digest'), 'url': url, 'title': manifest['title'],
                 'images': [{k: x[k] for k in ('loaded', 'width', 'height')} for x in rows],
                 'paragraphs_match': True, 'source_visible': self.page.locator('#js_view_source').is_visible(),
                 'ai_source': '内容由AI生成' in body, 'screenshot': str(self.directory/name)}
        require(proof['source_visible'], 'Reading-original entrance is not visible')
        if 'AI' in content.get('disclosure', ''):
            require(proof['ai_source'], 'Actual AI source annotation is missing')
        if url:
            self.public_proof = proof
        return proof

    def publication_guard(self):
        require(self.authorized_publish, 'Current publication authorization is required')
        manifest = self.guard(editor=True)
        batch = os.environ.get('MANYING_BATCH_JOB')
        if batch:
            job = ctl.read_json(batch)
            require(job.get('count') == 3 and job.get('state') == 'running', 'Inactive or invalid server batch')
            created = [p for p in self.root.glob('*/manifest.json') if p.parent.name not in job['before']]
            require(self.directory.name not in job['before'] and len(created) <= job['count'],
                    'Article is outside this server batch or batch exceeds three articles')
        require(manifest['status'] == 'editor_saved', 'In-flight/published articles must be checked in history, never resubmitted')
        intent_path = self.directory / 'rpa-submit-intent.json'
        prior = ctl.read_json(intent_path) if intent_path.exists() else self.prior()
        require(not (prior.get('op') == 'submit' and prior.get('status') in {'pending', 'uncertain', 'completed'}), 'An earlier submit may have happened; inspect publication history')
        require(ctl.check(self.directory, 'publish')['ok'], 'Publication preparation gates failed')
        require(archive.check_article(self.root, self.directory)['ok'], 'Duplicate checks failed')
        return manifest

    def publication_open(self):
        self.publication_guard()
        def action():
            self.click_control('button.mass_send', '发表')
            self.page.locator(DIALOG).wait_for(timeout=15000)
            return self.publication_settings()
        return self.mutate('publication-open', action)

    def publication_settings(self):
        dialog = visible_dialog(self.page)
        labels = dialog.locator('label.weui-desktop-form__label:visible').all_inner_texts()
        states = dialog.locator('input[type=checkbox]:visible').evaluate_all('els=>els.map(e=>e.checked)')
        require(labels in [['群发通知', '分组通知', '定时发表'], ['群发通知', '定时发表']] and len(states) == len(labels), 'Publication settings changed; inspect current DOM')
        return {'settings': dict(zip(labels, states)), 'text': dialog.inner_text()}

    def publication_config(self, notify=False):
        self.publication_guard()
        require(isinstance(notify, bool), 'notify must be a boolean')
        require(not notify or self.authorized_notify, 'Extra notification requires effective notification authorization')
        def action():
            for label, wanted in [('群发通知', notify), ('分组通知', False), ('定时发表', False)]:
                state = self.publication_settings()['settings']
                if label in state and state[label] != wanted:
                    labels = list(state)
                    visible_dialog(self.page).locator('label.weui-desktop-switch:visible').nth(labels.index(label)).click()
                    self.page.wait_for_function("""data=>{
                      const dialogs=Array.from(document.querySelectorAll('.weui-desktop-dialog')).filter(e=>e.getClientRects().length);
                      if(dialogs.length!==1)return false;const d=dialogs[0];
                      const labels=Array.from(d.querySelectorAll('label.weui-desktop-form__label')).filter(e=>e.getClientRects().length).map(e=>e.innerText.trim());
                      const i=labels.indexOf(data.label);const inputs=Array.from(d.querySelectorAll('input[type=checkbox]')).filter(e=>e.getClientRects().length);
                      return i<0?data.wanted===false:inputs[i]?.checked===data.wanted;
                    }""", arg={'label': label, 'wanted': wanted}, timeout=5000)
            settings = self.publication_settings()
            self.config = {'notify': notify, 'digest': self.manifest().get('render_digest'), 'page': self.page}
            return settings
        return self.mutate('publication-config', action)

    def publication_review(self):
        self.publication_guard()
        require(self.config and self.config['page'] is self.page, 'Read back publication settings first')
        expected = self.config['notify']
        state = self.publication_settings()['settings']
        require(state.get('群发通知') == expected and not state.get('分组通知', False) and not state.get('定时发表'), 'Publication settings differ')
        def action():
            self.click_control(DIALOG+' button:text-is("发表")', '发表')
            self.page.locator(DIALOG+' button:text-is("继续发表")').wait_for(timeout=15000)
            text = visible_dialog(self.page).inner_text()
            require(('未开启群发通知' if not expected else '已开启群发通知') in text, 'Confirmation notification scope differs')
            return {'confirmation': text, 'title': self.manifest()['title'], 'submit_sent': False}
        return self.mutate('publication-review', action)

    def submit(self):
        manifest = self.publication_guard()
        require(self.config and self.config['page'] is self.page and self.config['digest'] == manifest.get('render_digest'), 'Saved version/configuration changed')
        confirmation = visible_dialog(self.page).inner_text()
        require(('未开启群发通知' if not self.config['notify'] else '已开启群发通知') in confirmation, 'Unexpected confirmation scope')
        node = r.one(self.page, DIALOG+' button:text-is("继续发表")')
        require(r.label(node) == '继续发表', 'Final button changed; no submit sent')
        self.last_shot = None
        self.checkpoint('submit', 'pending', render_digest=manifest.get('render_digest'))
        try:
            node.click(timeout=10000)
        except Exception:
            # Once the real click is attempted, a timeout cannot prove it was unsent.
            ctl.record(NS(dir=str(self.directory), event='submit-requested', note='已尝试当前稿件最终提交控件，调用结果不明；只读核查后台，不重发', url=None))
            ctl.record(NS(dir=str(self.directory), event='uncertain', note='最终提交可能生效但回执不明，保留当前会话并核查发表历史', url=None))
            self.checkpoint('submit', 'uncertain', render_digest=manifest.get('render_digest'))
            raise
        ctl.record(NS(dir=str(self.directory), event='submit-requested', note='本地RPA已单次操作当前稿件最终继续发表控件；通知范围及即时设置已读回', url=None))
        self.checkpoint('submit', 'completed', render_digest=manifest.get('render_digest'))
        return {'state': 'submitted', 'published': False, 'next': 'Observe current page and publication history; never click submit again'}

    def history(self):
        self.guard()
        rows = history_rows(self.page)
        matches = [x for x in rows if r.norm(x['title']) == r.norm(self.manifest()['title'])]
        require(len(matches) <= 1, 'Multiple matching publication records; inspect them before proceeding')
        self.history_proof = matches[0] if matches else None
        self.history_checked = True
        if matches and matches[0]['status'] in {'审核中', '等候发表', '发表中', '定时发表'}:
            with ctl.locked(self.directory):
                manifest = ctl.load_manifest(self.directory)
                if manifest['status'] != 'published':
                    manifest['platform_status'] = matches[0]['status']
                    ctl.save_event(self.directory, manifest, 'platform-observed', '后台实际状态：'+matches[0]['status'])
        return {'matches': matches, 'visible_records': len(rows), 'not_found_on_current_page': not bool(matches)}

    def open_public(self):
        self.guard()
        # Re-read immediately; do not trust a stale completed status from another page.
        self.history()
        proof = self.history_proof
        require(proof and proof['status'] == '已发表' and proof['public_url'], 'A real published backend record is required')
        link = self.page.locator('a.weui-desktop-mass-appmsg__title:visible').filter(has_text=self.manifest()['title'])
        require(link.count() == 1, 'Publication link is ambiguous')
        with self.context.expect_page(timeout=15000) as opened:
            link.click()
        self.page = opened.value
        self.page.wait_for_load_state('domcontentloaded', timeout=20000)
        self.page.locator('#js_content').wait_for(timeout=30000)
        require(safe_url(self.page.url) == proof['public_url'], 'Opened page differs from the published record')
        return {'public_url': proof['public_url'], 'observe_next': True}

    def record_published(self, note):
        manifest = self.manifest()
        require(ctl.check(self.directory)['ok'], 'Local content changed after public-page verification')
        proof, history = self.public_proof, self.history_proof
        require(proof and history and history['status'] == '已发表', 'Backend and public-page proof are both required')
        require(proof['digest'] == manifest.get('render_digest') and proof['title'] == manifest['title'] and
                proof['url'] == history['public_url'] == safe_url(self.page.url), 'Publication proof no longer matches')
        note = ctl.safe_note(note)
        if manifest['status'] == 'published':
            require(manifest.get('public_url') == proof['url'], 'Recorded public URL differs')
            return {'state': 'already_published', 'public_url': proof['url']}
        # Historical reconciliation records reality directly; never invent submit/check events.
        with ctl.locked(self.directory):
            manifest = ctl.load_manifest(self.directory, mutable=True)
            previous_status = manifest['status']
            screenshot = self.directory / 'wechat-public.png'
            require(screenshot.is_file(), 'Public-page screenshot must exist before recording publication')
            ctl.write_json(self.directory / 'public-verification.json', {
                'schema_version': 1, 'title': proof['title'], 'public_url': proof['url'],
                'render_digest': proof['digest'], 'backend_status': history['status'],
                'paragraphs_match': proof['paragraphs_match'], 'images': proof['images'],
                'source_visible': proof['source_visible'], 'ai_source': proof['ai_source'],
                'screenshot_sha256': hashlib.sha256(screenshot.read_bytes()).hexdigest(),
                'observed_at': ctl.now(),
            })
            manifest.update(status='published', public_url=proof['url'], published_at_observed=ctl.now(), platform_status='已发表')
            header = self.page.locator('body').inner_text()[:500]
            stamp = re.search(r'(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日\s*(\d{1,2}):(\d{2})', header)
            if stamp:
                manifest['published_at'] = datetime(*map(int, stamp.groups()), tzinfo=timezone(timedelta(hours=8))).isoformat()
            ctl.save_event(self.directory, manifest, 'published' if previous_status in INFLIGHT else 'published-reconciled', note)
        return {'state': 'published', 'public_url': proof['url'], 'published_at': manifest.get('published_at')}
