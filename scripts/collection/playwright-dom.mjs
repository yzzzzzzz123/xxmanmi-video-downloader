// Public DOM only. This function is serialized directly to an isolated browser.
export function readListView() {
  const visible = e => !!e && !!e.getClientRects().length &&
    getComputedStyle(e).visibility !== 'hidden';
  const search = visible(document.querySelector('#search'));
  const panel = document.querySelector(search ? '#searchList' : '#like');
  const radios = [...document.querySelectorAll('#like input[type=radio][id]')]
    .map(e => { const label = document.querySelector(`label[for="${e.id}"]`);
      return {id:e.id, name:e.name, value:e.value, checked:e.checked,
        text:label?.textContent.trim() || '', visible:visible(label)}; })
    .filter(e => e.visible && e.text);
  const source = search
    ? [...document.querySelectorAll('#search botton, #search button')]
      .find(e => visible(e) && e.classList.contains('weui-btn_current'))?.textContent.trim() || ''
    : radios.find(e => e.name === 'newNum' && e.checked)?.text || '';
  const items = [...(panel?.querySelectorAll('a[href]') || [])].filter(visible)
    .filter(a => /^\/video\d+\/[^/]+\.html$/.test(new URL(a.href).pathname))
    .map(a => { const bd = a.querySelector('.weui-media-box__bd');
      const title = bd ? [...bd.childNodes].filter(n => n.nodeType === Node.TEXT_NODE)
        .map(n => n.textContent).join('').trim() : a.textContent.trim();
      const desc = a.querySelector('.weui-media-box__desc')?.textContent.trim() || '';
      return {url:a.href,title,status:desc === title ? '' : desc,source,
        category:'',query:search ? document.querySelector('#searchInput')?.value || '' : ''}; });
  const loading = [...document.querySelectorAll('[id*=loading],[class*=loading],.weui-toast')]
    .some(e => visible(e) && /加载|搜索|loading/i.test(e.textContent));
  const pagers = [...document.querySelectorAll('a,button,botton,[role=button]')]
    .filter(e => visible(e) && /^(下一页|下一頁|加载更多|更多|next|load more)$/i.test(e.textContent.trim()))
    .map(e => ({text:e.textContent.trim(),href:e.getAttribute('href') || '',id:e.id || '',
      disabled:!!e.disabled || e.getAttribute('aria-disabled') === 'true'}));
  const tabs = [...document.querySelectorAll('.weui-tabbar [data-id]')]
    .filter(visible).map(e => ({value:e.getAttribute('data-id'),text:e.textContent.trim(),
      selected:e.classList.contains('weui-bar__item_on')}));
  return {url:location.href,readyState:document.readyState,view:search?'search':'latest',tabs,
    panelVisible:visible(panel),source,radios,items,loading,pagers,
    explicitEmpty:!!panel && /暂无(?:相关)?(?:结果|数据)|没有(?:找到|搜索到)|no results/i.test(panel.textContent)};
}

if (process.argv[2] === 'reader') process.stdout.write(JSON.stringify({code:readListView.toString()}));
