'use strict';
// A document owns its save queue. Background workbench polling never rebuilds it.
let activeDocumentEditor = null;
const editorClone = value => JSON.parse(JSON.stringify(value));
const newBlockId = () => crypto.randomUUID().replaceAll('-', '');
const cleanReaderCaption = value => {
  const text = String(value || '').trim();
  return /^(原报道配图|来源报道配图|相关公开内容封面|来源配图)/.test(text) || /终审核对|终审时核对|转载依据未核实|发布前核对|非经核验/.test(text) ? '' : text;
};

class DocumentEditor {
  constructor(response) {
    this.response = response;
    this.id = response.article.id;
    this.doc = editorClone(response.document);
    this.revision = response.article.revision;
    this.sequence = this.savedSequence = 0;
    this.history = []; this.future = [];
    this.composing = false; this.blocked = false; this.exporting = false;
    this.selected = this.doc.blocks[0]?.id;
    this.sessionId = newBlockId();
    this.storageKey = `ce-document:${this.id}:${this.sessionId}`;
    this.view = 'edit'; this.width = 'desktop';
  }
  photo(id) { return this.response.photos.find(p => p.id === id); }
  coverId() {
    const images = this.doc.blocks.filter(b => b.type === 'image');
    return (this.doc.cover.mode === 'manual' && images.find(b => b.asset_id === this.doc.cover.asset_id)?.asset_id) || images[0]?.asset_id;
  }
  caption(block) {
    const photo = this.photo(block.asset_id);
    let caption = cleanReaderCaption(block.caption);
    if (photo?.data.photo_role === 'file_photo') caption = !caption || caption.startsWith('File:') ? '资料图' : (/资料图|资料照/.test(caption) ? caption : caption + '（资料图）');
    return [caption, photo?.credit_suffix].filter(Boolean).join(' · ');
  }
  async request(url, body) {
    const res = await fetch(url, {method: body === undefined ? 'GET' : 'POST', headers: {'X-App-Token': token, ...(body === undefined ? {} : {'Content-Type':'application/json'})}, ...(body === undefined ? {} : {body: JSON.stringify(body)})});
    const result = await res.json();
    if (!res.ok) { const err = new Error(result.error || '请求失败'); err.code = result.code; err.status = res.status; throw err; }
    return result;
  }
  mount() {
    document.body.classList.add('document-editing');
    page = 'editor'; editorDirty = false; currentArticle = this.response;
    $('#modal').close();
    $('#breadcrumb').textContent = '稿件中心 / 图文编辑';
    this.root = $('#main');
    this.root.dataset.view='edit';
    this.root.classList.remove('document-full-preview');
    this.root.innerHTML = `<section class="document-shell" aria-label="图文编辑器">
      <header class="document-header"><div class="document-heading"><button class="button ghost" data-ed="back">← 稿件中心</button><div><span class="eyebrow">${esc(this.response.account.name)} · 图文编辑</span><strong id="document-heading-title"></strong></div></div><div class="document-actions"><span id="document-save-status" role="status" aria-live="polite">已保存</span><button class="button ghost small" data-ed="undo" aria-label="撤销上一步">↶ 撤销</button><button class="button ghost small" data-ed="redo" aria-label="重做">↷</button><button class="button small" data-ed="retry" hidden>重试保存</button><button class="button small" data-ed="full-preview">全屏预览</button><button class="button primary" data-ed="export">导出 Word</button></div></header>
      <div id="document-publication">${typeof Publishing==='undefined'?'':Publishing.strip(this.id,this.response.publication,this.revision)}</div>
      <div id="document-alert" role="alert" hidden></div>
      <div class="document-toolbar"><div class="document-view-tabs" role="tablist"><button role="tab" data-ed="tab-edit" aria-selected="true">编辑</button><button role="tab" data-ed="tab-preview" aria-selected="false">预览</button></div><span class="document-count"></span><div class="row-actions"><button class="button ghost small" data-ed="materials">▧ 图片素材 <span id="material-count"></span></button><button class="button ghost small" data-ed="sources">资料与建议</button><button class="button ghost small" data-ed="ai">✦ AI 改稿</button><a class="button ghost small" href="https://mp.toutiao.com/profile_v4/index" target="_blank" rel="noopener noreferrer">头条后台 ↗</a></div></div>
      <div class="document-columns"><section class="document-edit-pane" aria-label="编辑正文"><label class="document-title-label">文章标题<textarea id="document-title" rows="1" maxlength="200" placeholder="给文章一个准确、有看点的标题"></textarea></label><div id="document-blocks"></div><button class="button document-add" data-ed="add-text">＋ 添加文字模块</button><p class="document-footnote">停止输入后自动保存 · 图片可拖动排序，也可使用上移／下移 · 保存即可导出</p></section>
      <section class="document-preview-pane" aria-label="阅读预览"><div class="document-preview-tools"><strong>阅读预览</strong><div><button class="button ghost small" data-ed="width-mobile" aria-pressed="false">手机 390</button><button class="button ghost small" data-ed="width-desktop" aria-pressed="true">桌面 680</button><button class="button ghost small document-close-full" data-ed="full-preview">退出全屏</button></div></div><div class="document-preview-scroll"><div id="document-cover"></div><article id="document-reader" class="reader-desktop"></article><p class="document-footnote">预览检查阅读顺序；Word 分页以实际导出文件为准。</p></div></section></div>
      <aside id="document-drawer" class="document-drawer" hidden><div class="document-drawer-header"><h2 id="document-drawer-title"></h2><button class="icon-button" data-ed="drawer-close" aria-label="关闭侧栏">×</button></div><div id="document-drawer-body"></div></aside></section>`;
    $('#document-title').value = this.doc.title;
    this.drawBlocks(); this.updatePreview(); this.showRecovery();
    this.root.onclick = e => this.click(e);
    this.root.oninput = e => this.input(e);
    this.root.onsubmit = e => { if(e.target.id==='document-ai-settings'){e.preventDefault();e.stopPropagation();} };
    this.root.onfocusin = e => { const row = e.target.closest('[data-block]'); if (row) this.select(row.dataset.block); };
    this.root.oncompositionstart = () => { this.composing = true; clearTimeout(this.timer); };
    this.root.oncompositionend = () => { this.composing = false; this.queueSave(); };
    this.root.ondragstart = e => this.dragStart(e);
    this.root.ondragover = e => { const zone = e.target.closest('[data-drop]'); if (zone) { e.preventDefault(); zone.classList.add('drag-over'); e.dataTransfer.dropEffect = 'move'; } };
    this.root.ondragleave = e => e.target.closest('[data-drop]')?.classList.remove('drag-over');
    this.root.ondrop = e => this.drop(e);
    this.root.onpointerdown = e => this.pointerStart(e);
    this.root.onpointermove = e => this.pointerMove(e);
    this.root.onpointerup = e => this.pointerEnd(e);
    this.root.onpointercancel = () => this.pointerEnd(null);
    history.replaceState({}, '', '?article=' + this.id);
    this.autoHeight();
    this.resizeObserver=new ResizeObserver(entries=>{const width=entries[0]?.contentRect.width;if(width>0&&width!==this.lastEditWidth){this.lastEditWidth=width;const pane=entries[0].target,scroll=pane.scrollTop;this.autoHeight();pane.scrollTop=scroll;}});
    this.resizeObserver.observe($('.document-edit-pane',this.root));
  }
  select(id) {
    this.selected = id;
    $$('[data-block]', this.root).forEach(el => el.classList.toggle('selected-block', el.dataset.block === id));
  }
  blockMarkup(block, index) {
    const buttons = `<div class="block-controls"><span draggable="false" class="block-handle" data-drag="${block.id}" title="拖动排序" aria-label="拖动模块排序">⠿</span><span>${block.type === 'image' ? '图片' : '文字'}</span><div><button data-ed="up" data-block-id="${block.id}" aria-label="上移模块" ${index===0?'disabled':''}>↑</button><button data-ed="down" data-block-id="${block.id}" aria-label="下移模块" ${index===this.doc.blocks.length-1?'disabled':''}>↓</button><button data-ed="remove" data-block-id="${block.id}" aria-label="移除模块">×</button></div></div>`;
    const image = this.photo(block.asset_id);
    return `<article class="document-block ${block.id===this.selected?'selected-block':''}" data-block="${block.id}">${buttons}${block.type === 'text' ? `<textarea class="block-text" data-text="${block.id}" aria-label="正文文字模块 ${index+1}" placeholder="写下这一段…">${esc(block.text)}</textarea>` : `<div class="image-block"><img src="${esc(image?.preview_url || '')}" alt="正文配图"><span class="image-cover-badge" ${this.coverId()===block.asset_id?'':'hidden'}>${this.doc.cover.mode==='auto'?'首图封面':'指定封面'}</span></div><input class="block-caption" data-caption="${block.id}" aria-label="图片图注" placeholder="添加图注（可留空）" value="${esc(block.caption)}"><div class="image-block-actions"><button class="button ghost small" data-ed="replace" data-block-id="${block.id}">替换图片</button><details><summary>更多操作</summary><div><button class="button ghost small" data-ed="cover" data-block-id="${block.id}">设为封面</button><button class="button ghost small" data-ed="cover-auto">封面跟随首图</button><button class="button ghost small" data-ed="photo-details" data-block-id="${block.id}">素材详情</button></div></details></div>`}</article>`;
  }
  drawBlocks() {
    const container = $('#document-blocks'); if (!container) return;
    container.innerHTML = this.doc.blocks.map((b,i)=>`<div class="document-drop" data-drop="${i}"><button data-ed="insert-text" data-index="${i}" aria-label="在此插入文字">＋</button></div>${this.blockMarkup(b,i)}`).join('') + `<div class="document-drop" data-drop="${this.doc.blocks.length}"><span>拖入图片或模块</span></div>`;
    this.autoHeight();
  }
  autoHeight() { $$('textarea.block-text, #document-title', this.root).forEach(el => { el.style.height = 'auto'; el.style.height = el.scrollHeight + 'px'; }); }
  updatePublication(summary) {
    this.response.publication=summary;
    if(typeof Publishing!=='undefined'&&$('#document-publication'))$('#document-publication').innerHTML=Publishing.strip(this.id,summary,this.revision);
  }
  updatePreview() {
    const reader = $('#document-reader'); if (!reader) return;
    reader.className = 'reader-' + this.width;
    reader.innerHTML = (this.response.topic.is_demo ? '<p class="reader-demo">合成演示 · 请勿当作新闻发布</p>' : '') + `<h1>${esc(this.doc.title)}</h1>` + this.doc.blocks.map(b => b.type==='text' ? `<p>${esc(b.text).replaceAll('\n','<br>')}</p>` : `<figure><img src="${esc(this.photo(b.asset_id)?.preview_url || '')}" alt="文章配图">${this.caption(b)?`<figcaption>${esc(this.caption(b))}</figcaption>`:''}</figure>`).join('');
    const cover = this.photo(this.coverId());
    $('#document-cover').innerHTML = `<div class="reader-cover-card"><div><span class="eyebrow">独立封面 · ${this.doc.cover.mode==='manual'?'指定图片':'跟随正文首图'}</span><p>${cover?'封面单独交付，不重复插入正文':'插入第一张图片后自动作为封面'}</p></div>${cover?`<img src="${esc(cover.preview_url)}" alt="封面预览">`:''}</div>`;
    $('.document-count').textContent = `${this.doc.blocks.reduce((n,b)=>n+(b.text?.length||0),0)} 字 · ${this.doc.blocks.filter(b=>b.type==='image').length} 张图片`;
    $('#document-heading-title').textContent = this.doc.title || '未命名稿件';
    $('#material-count').textContent = this.response.photos.length;
    $$('[data-block]', this.root).forEach(el => { const b=this.doc.blocks.find(x=>x.id===el.dataset.block), badge=$('.image-cover-badge',el); if(badge){badge.hidden=b.asset_id!==this.coverId();badge.textContent=this.doc.cover.mode==='auto'?'首图封面':'指定封面';} });
  }
  mutate(action, redraw=true) {
    if (this.exporting) return;
    this.history.push(editorClone(this.doc)); if(this.history.length>80)this.history.shift(); this.future=[];
    action(this.doc); this.changed(redraw);
  }
  changed(redraw=true) {
    this.sequence++; this.persistLocal(); this.setStatus('等待保存');
    if(redraw)this.drawBlocks(); this.updatePreview(); this.queueSave();
  }
  input(e) {
    if (e.target.id==='document-title') this.mutate(d=>d.title=e.target.value,false);
    else if(e.target.dataset.text) this.mutate(d=>d.blocks.find(b=>b.id===e.target.dataset.text).text=e.target.value,false);
    else if(e.target.dataset.caption) this.mutate(d=>d.blocks.find(b=>b.id===e.target.dataset.caption).caption=e.target.value,false);
    this.autoHeight();
  }
  persistLocal() {
    try { localStorage.setItem(this.storageKey, JSON.stringify({article_id:this.id,revision:this.revision,document:this.doc,at:Date.now()})); }
    catch { this.localUnavailable=true; this.alert('浏览器本地草稿存储不可用，请保持页面打开并及时保存。'); }
  }
  queueSave() {
    clearTimeout(this.timer);
    if(this.composing||this.blocked)return;
    this.timer=setTimeout(()=>this.flush().catch(()=>{}),800);
  }
  setStatus(label,error=false) {
    const status=$('#document-save-status'); if(!status)return;
    status.textContent=label; status.classList.toggle('save-error',error);
    $('[data-ed="retry"]').hidden=!error;
  }
  async flush() {
    clearTimeout(this.timer);
    if(this.composing)throw new Error('请先完成当前输入法组词再操作');
    if(this.blocked)throw new Error(this.saveError || '请先处理保存失败');
    if(this.saving){await this.saving;if(this.savedSequence!==this.sequence)return this.flush();return;}
    this.saving=(async()=>{
      while(this.savedSequence!==this.sequence){
        if(this.composing)break;
        const sequence=this.sequence, document=editorClone(this.doc), revision=this.revision;
        this.setStatus('保存中…');
        try{
          const result=await this.request('/api/articles/'+this.id+'/document',{base_revision:revision,document});
          this.revision=result.revision;this.response.article.revision=result.revision;this.savedSequence=sequence;
          if(result.publication)this.updatePublication(result.publication);
          if(sequence===this.sequence){this.doc=result.document;try{localStorage.removeItem(this.storageKey);if(this.recoveredKey)localStorage.removeItem(this.recoveredKey);}catch{}$$('[data-caption]',this.root).forEach(input=>{const value=this.doc.blocks.find(b=>b.id===input.dataset.caption)?.caption;if(value!==undefined&&input.value!==value)input.value=value;});this.updatePreview();}
          else this.persistLocal();
          if(result.warning)toast(result.warning);
        }catch(error){
          this.blocked=true;this.saveError=error.message;this.persistLocal();this.setStatus(error.code==='revision_conflict'?'版本冲突 · 草稿已保留':'保存失败 · 草稿已保留',true);
          this.alert(error.message+ '。当前编辑仍保留在页面及本地草稿。',error.code==='revision_conflict'?'conflict':'save');throw error;
        }
      }
      this.setStatus(this.savedSequence===this.sequence?'已保存 · v'+this.revision:'等待输入完成');
    })();
    try{await this.saving;}finally{this.saving=null;}
  }
  alert(message,kind='notice') {
    const el=$('#document-alert');if(!el)return;el.hidden=false;
    el.innerHTML=`<span>${esc(message)}</span>${kind==='conflict'?'<button class="button small" data-ed="compare">比较服务器版本</button><button class="button small" data-ed="keep-current">以当前编辑为准保存</button>':''}${kind==='save'?'<button class="button small" data-ed="retry">重试保存</button>':''}<button class="button ghost small" data-ed="backup">下载本地草稿</button><button class="button ghost small" data-ed="dismiss-alert">收起</button>`;
  }
  showRecovery() {
    try{
      const drafts=Object.keys(localStorage).filter(k=>k.startsWith('ce-document:'+this.id+':')).flatMap(key=>{try{return [{key,...JSON.parse(localStorage.getItem(key))}];}catch{return [];}}).filter(d=>d.document&&JSON.stringify(d.document)!==JSON.stringify(this.doc)).sort((a,b)=>b.at-a.at);
      if(!drafts.length)return;
      this.recovery=drafts[0];const el=$('#document-alert');el.hidden=false;
      el.innerHTML=`<span>发现本地未保存草稿（${esc(new Date(this.recovery.at).toLocaleString())}），当前展示服务器版本。</span><button class="button small" data-ed="restore">恢复草稿</button><button class="button ghost small" data-ed="discard-recovery">忽略此草稿</button>`;
    }catch{}
  }
  async export(skipMissing=false,zip=false) {
    if(this.exporting)return;
    try{
      await this.flush();
      this.exporting=true;
      const revision=this.revision;
      this.root.classList.add('document-exporting');
      this.exportFocus=document.activeElement;
      $('.document-edit-pane',this.root).inert=true;
      $('#document-drawer').inert=true;
      $('[data-ed="export"]').textContent='正在导出…';
      if(typeof Publishing!=='undefined'&&this.response.publication){
        const result=await Publishing.download(this.id,revision,zip,skipMissing);
        if(!result.recorded){this.alert('文档已交付下载，但导出进度尚未记录。可重试记录，不必重新下载。');$('#document-alert').insertAdjacentHTML('beforeend','<button class="button small" data-ed="retry-export-record">重试记录导出进度</button>');return;}
      }else{
        const res=await fetch(`/api/articles/${this.id}/${zip?'export':'toutiao.docx'}?revision=${revision}${skipMissing?'&skip_missing=1':''}`,{headers:{'X-App-Token':token}});
        if(!res.ok){const error=await res.json();throw new Error(error.error||'导出失败');}
        const blob=await res.blob(),url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=`${zip?'图文包':'头条图文'}-${this.id.slice(0,8)}.${zip?'zip':'docx'}`;link.click();setTimeout(()=>URL.revokeObjectURL(url),10000);
      }
      toast('已导出 v'+revision+'；在头条发布后，可点击“标记已发布”');
    }catch(error){
      this.alert(error.message,'save');
      if(error.message.includes('图片无法读取'))$('#document-alert').insertAdjacentHTML('beforeend','<button class="button small" data-ed="export-skip">明确跳过缺失图片导出</button>');
    }finally{this.exporting=false;this.root.classList.remove('document-exporting');$('.document-edit-pane',this.root).inert=false;$('#document-drawer').inert=false;$('[data-ed="export"]').textContent='导出 Word';if(this.exportFocus?.isConnected)this.exportFocus.focus();}
  }
  insertPhoto(assetId,index=null) {
    if(this.doc.blocks.some(b=>b.type==='image'&&b.asset_id===assetId)){toast('这张图片已在正文中，可直接移动它');return;}
    const photo=this.photo(assetId); if(!photo)return;
    const target=this.replaceTarget;
    this.mutate(d=>{
      if(target){const block=d.blocks.find(b=>b.id===target);if(block){if(d.cover.mode==='manual'&&d.cover.asset_id===block.asset_id)d.cover.asset_id=assetId;block.asset_id=assetId;block.caption=cleanReaderCaption(photo.data.caption);}}
      else{const at=index??Math.max(0,d.blocks.findIndex(b=>b.id===this.selected)+1);const block={id:newBlockId(),type:'image',asset_id:assetId,caption:cleanReaderCaption(photo.data.caption)};d.blocks.splice(at,0,block);this.selected=block.id;}
    });
    this.replaceTarget=null;this.materials();
  }
  move(id,to) {
    this.mutate(d=>{const from=d.blocks.findIndex(b=>b.id===id);if(from<0)return;const [block]=d.blocks.splice(from,1);d.blocks.splice(Math.max(0,Math.min(to,d.blocks.length)),0,block);});
  }
  dragStart(e) {
    const handle=e.target.closest('[data-drag]'),asset=e.target.closest('[data-drag-asset]');
    if(!handle&&!asset)return;
    e.dataTransfer.setData('application/x-content-block',JSON.stringify(handle?{block:handle.dataset.drag}:{asset:asset.dataset.dragAsset}));e.dataTransfer.effectAllowed='move';
  }
  pointerStart(e) {
    if(e.button!==0||this.exporting)return;
    const source=e.target.closest('[data-drag], [data-drag-asset]');
    if(!source||source.disabled)return;
    this.pointerDrag={source,id:e.pointerId,x:e.clientX,y:e.clientY,block:source.dataset.drag,asset:source.dataset.dragAsset};
  }
  pointerMove(e) {
    const drag=this.pointerDrag;if(!drag||drag.id!==e.pointerId)return;
    if(!drag.moved&&Math.hypot(e.clientX-drag.x,e.clientY-drag.y)<6)return;
    if(!drag.moved){drag.moved=true;this.root.setPointerCapture(e.pointerId);this.root.classList.add('document-dragging');}
    e.preventDefault();
    const zone=document.elementFromPoint(e.clientX,e.clientY)?.closest('[data-drop]');
    $$('[data-drop]',this.root).forEach(el=>el.classList.toggle('drag-over',el===zone));
    drag.index=zone?Number(zone.dataset.drop):null;
    const pane=$('.document-edit-pane',this.root),rect=pane.getBoundingClientRect();
    if(e.clientY>rect.bottom-50)pane.scrollBy(0,16);
    if(e.clientY<rect.top+50)pane.scrollBy(0,-16);
  }
  pointerEnd(e) {
    const drag=this.pointerDrag;this.pointerDrag=null;if(!drag)return;
    this.root.classList.remove('document-dragging');
    $$('[data-drop]',this.root).forEach(el=>el.classList.remove('drag-over'));
    try{this.root.releasePointerCapture(drag.id);}catch{}
    if(!e||!drag.moved||drag.index===null||drag.index===undefined)return;
    this.suppressClickUntil=Date.now()+250;
    if(drag.block){const from=this.doc.blocks.findIndex(b=>b.id===drag.block);this.move(drag.block,drag.index-(from<drag.index?1:0));}
    else if(drag.asset)this.insertPhoto(drag.asset,drag.index);
  }
  drop(e) {
    const zone=e.target.closest('[data-drop]');if(!zone)return;e.preventDefault();zone.classList.remove('drag-over');
    try{const data=JSON.parse(e.dataTransfer.getData('application/x-content-block'));let to=Number(zone.dataset.drop);if(data.block){const from=this.doc.blocks.findIndex(b=>b.id===data.block);this.move(data.block,to-(from<to?1:0));}else if(data.asset)this.insertPhoto(data.asset,to);}catch{}
  }
  drawer(title,content) {$('#document-drawer-title').textContent=title;$('#document-drawer-body').innerHTML=content;$('#document-drawer').hidden=false;}
  materials() {
    const used=new Set(this.doc.blocks.filter(b=>b.type==='image').map(b=>b.asset_id));
    this.drawer('图片素材',`${this.replaceTarget?'<div class="notice">正在替换图片，点击下方一张未使用素材即可。</div>':'<p class="small muted">点击插入当前模块后，或拖入正文模块之间。移除的图片会回到这里。Agent补充的照片和数据图表都会保存在素材栏，不覆盖你的正文。</p>'}<div class="document-materials">${this.response.photos.slice().sort((a,b)=>(b.data.photo_role==='data_chart'?110:b.data.relevance_score||40)-(a.data.photo_role==='data_chart'?110:a.data.relevance_score||40)).map(p=>`<button class="material-tile" data-ed="insert-photo" data-asset="${p.id}" data-drag-asset="${p.id}" draggable="false" ${used.has(p.id)?'disabled':''}><img draggable="false" src="${esc(p.preview_url)}" alt="图片素材"><span>${p.data.photo_role==='data_chart'?'数据图表':p.data.photo_role==='file_photo'?'资料照／资料图':'报道图片'} · ${used.has(p.id)?'已在正文':'点击插入'} · ${p.data.width}×${p.data.height}</span><small>${esc(p.data.matched_subject||p.data.caption||'')} ${p.data.relevance_score?' · 相关性 '+p.data.relevance_score:''}</small></button>`).join('')||'<p>暂无素材。可让 Agent 按原报道重新检索配图。</p>'}</div><div class="section-gap"><label class="field"><span>补图关键词（可选，一行一个人物或队名）</span><textarea id="document-image-queries" placeholder="可留空，Agent自动识别人物／队伍；也可输入 Luka Doncic"></textarea></label><button class="button" data-ed="find-photos">✦ 补照片与数据图表</button><button class="button ghost small" data-ed="refresh-materials">刷新素材</button></div>`);
  }
  sources() {
    this.drawer('资料与编辑建议',`<p class="small muted">以下仅供编辑参考，不阻止保存与导出，也不进入正文。</p><details open><summary>检查建议 · ${this.response.quality.warnings.length} 项</summary><ul class="quality-list">${this.response.quality.warnings.map(x=>`<li>${esc(x)}</li>`).join('')}</ul></details><details class="section-gap"><summary>来源材料 · ${this.response.topic.data.facts.length} 条</summary>${this.response.topic.data.facts.map(f=>`<div class="evidence-item"><p>${esc(f.text)}</p><a href="${esc(f.url)}" target="_blank" rel="noopener noreferrer">查看原始出处 ↗</a>${f.original?`<p>${esc(f.original)}</p>`:''}</div>`).join('')}</details><details class="section-gap"><summary>编辑记录</summary><p>${esc((this.response.article.data.notes||[]).join('\n'))}</p></details><div class="section-gap"><button class="button small" data-ed="export-zip">导出图文资料包</button></div>`);
  }
  ai() {
    this.drawer('AI 改稿建议',`<p class="small muted">先保存当前稿件，AI 生成独立建议供你比较与采用。你可以继续编辑正文。</p><form id="document-ai-settings">${aiFields(this.response.production_defaults.ai,false)}${contentDepthFields(this.response.article.data.writing_policy?.level||this.response.production_defaults.content_depth)}</form><label class="field"><span>希望怎么修改</span><textarea id="document-ai-feedback" placeholder="例如：展开关键进攻回合的作用，保持轻松自然的体育作者口吻。"></textarea></label><button class="button primary" data-ed="request-ai">生成改稿建议</button><button class="button ghost small" data-ed="refresh-suggestions">刷新建议</button><div id="document-suggestions">${this.suggestionMarkup()}</div>`);
  }
  suggestionMarkup() {
    return (this.response.suggestions||[]).map(s=>`<section class="document-suggestion"><strong>基于 v${s.base_revision} 的建议 ${s.base_revision!==this.revision?'· 当前稿件已有修改':''}</strong><h3>${esc(s.data.title)}</h3>${s.data.paragraphs.map(p=>`<p>${esc(p.text)}</p>`).join('')}<button class="button primary small" data-ed="adopt" data-suggestion="${s.id}" ${s.base_revision!==this.revision?'disabled':''}>采用此建议</button><button class="button ghost small" data-ed="copy-suggestion" data-suggestion="${s.id}">复制建议以便手动合并</button></section>`).join('')||'<p class="muted section-gap">暂无待采用的改稿建议。</p>';
  }
  async refreshData() {
    const response=await this.request('/api/articles/'+this.id);
    this.response.photos=response.photos;this.response.suggestions=response.suggestions;this.response.quality=response.quality;
    this.updatePreview();return response;
  }
  async watchRun(id) {
    clearTimeout(this.runTimer);
    if(activeDocumentEditor!==this)return;
    try{
      const response=await this.request('/api/agent/runs/'+id);const run=response.run||response;
      if(['queued','running','waiting_external'].includes(run.state)){this.runTimer=setTimeout(()=>this.watchRun(id),5000);return;}
      await this.refreshData();
      if($('#document-suggestions'))$('#document-suggestions').innerHTML=this.suggestionMarkup();
      toast(run.state==='failed'?(run.error||'任务未完成，原稿已保留'):'Agent 已完成，可刷新素材或查看改稿建议',run.state==='failed');
    }catch{this.runTimer=setTimeout(()=>this.watchRun(id),10000);}
  }
  async click(e) {
    if(Date.now()<(this.suppressClickUntil||0))return;
    const button=e.target.closest('[data-ed]');if(!button)return;
    e.preventDefault();const action=button.dataset.ed,id=button.dataset.blockId;
    if(this.exporting)return;
    try{
      if(action==='back'){await this.flush();if(!this.exporting)await this.leave();return;}
      if(action==='undo'||action==='redo'){const from=action==='undo'?this.history:this.future,to=action==='undo'?this.future:this.history;if(from.length){to.push(editorClone(this.doc));this.doc=from.pop();$('#document-title').value=this.doc.title;this.changed();}return;}
      if(action==='retry'){this.blocked=false;await this.flush();$('#document-alert').hidden=true;return;}
      if(action==='retry-export-record'){const failed=await Publishing.retryReceipts();if(failed)throw new Error('仍未能记录导出进度，请检查连接后重试');$('#document-alert').hidden=true;toast('导出进度已记录');return;}
      if(action==='export'||action==='export-skip'||action==='export-zip'){await this.export(action==='export-skip',action==='export-zip');return;}
      if(action==='tab-edit'||action==='tab-preview'){this.view=action==='tab-edit'?'edit':'preview';this.root.dataset.view=this.view;$$('[role=tab]',this.root).forEach(b=>b.setAttribute('aria-selected',b.dataset.ed===action));return;}
      if(action.startsWith('width-')){this.width=action.slice(6);this.updatePreview();$$('[data-ed^="width-"]',this.root).forEach(b=>b.setAttribute('aria-pressed',b.dataset.ed===action));return;}
      if(action==='full-preview'){this.root.classList.toggle('document-full-preview');return;}
      if(action==='drawer-close'){$('#document-drawer').hidden=true;this.replaceTarget=null;return;}
      if(action==='dismiss-alert'){$('#document-alert').hidden=true;return;}
      if(action==='materials'){this.replaceTarget=null;this.materials();return;}
      if(action==='sources'){await this.refreshData();this.sources();return;}
      if(action==='ai'){this.ai();return;}
      if(action==='up'||action==='down'){const at=this.doc.blocks.findIndex(b=>b.id===id);this.move(id,at+(action==='up'?-1:1));return;}
      if(action==='remove'){
        const block=this.doc.blocks.find(b=>b.id===id);if(this.doc.blocks.length===1){toast('请至少保留一个文字模块');return;}
        this.mutate(d=>{d.blocks=d.blocks.filter(b=>b.id!==id);if(block.type==='image'&&d.cover.mode==='manual'&&d.cover.asset_id===block.asset_id){d.cover={mode:'auto',asset_id:null};toast('指定封面已移除，恢复首图封面');}});return;
      }
      if(action==='add-text'||action==='insert-text'){const block={id:newBlockId(),type:'text',text:'',kind:'analysis',claim_ids:[]};this.mutate(d=>d.blocks.splice(action==='add-text'?d.blocks.length:Number(button.dataset.index),0,block));$(`[data-text="${block.id}"]`)?.focus();return;}
      if(action==='insert-photo'){this.insertPhoto(button.dataset.asset);return;}
      if(action==='replace'){this.replaceTarget=id;this.materials();return;}
      if(action==='cover'){this.mutate(d=>d.cover={mode:'manual',asset_id:d.blocks.find(b=>b.id===id).asset_id});return;}
      if(action==='cover-auto'){this.mutate(d=>d.cover={mode:'auto',asset_id:null});return;}
      if(action==='photo-details'){const p=this.photo(this.doc.blocks.find(b=>b.id===id).asset_id);this.drawer('素材详情',`<img class="material-detail-image" src="${esc(p.preview_url)}" alt="素材"><p>${esc(p.data.caption)}</p><p>${esc(p.data.credit)} · ${esc(p.data.license)}</p><a href="${esc(p.source_url)}" target="_blank" rel="noopener noreferrer">查看图片原报道 ↗</a>`);return;}
      if(action==='refresh-materials'){await this.refreshData();this.materials();return;}
      if(action==='find-photos'){const queries=$('#document-image-queries').value.split('\n').map(x=>x.trim()).filter(Boolean);await this.flush();button.disabled=true;const run=await this.request('/api/agent/illustrate',{account_id:this.response.article.account_id,article_id:this.id,base_revision:this.revision,image_queries:queries});this.watchRun(run.id);toast('Agent 补图任务已加入队列，完成后刷新素材即可');return;}
      if(action==='request-ai'){const feedback=$('#document-ai-feedback').value,choices=$('#document-ai-settings'),ai=readAI(choices),depth=$('[name=content_depth]',choices).value;await this.flush();button.disabled=true;const run=await this.request('/api/agent/revise',{account_id:this.response.article.account_id,article_id:this.id,base_revision:this.revision,feedback,ai,content_depth:depth});this.watchRun(run.id);toast('AI 将生成独立建议，不覆盖当前编辑');button.disabled=false;return;}
      if(action==='refresh-suggestions'){await this.refreshData();$('#document-suggestions').innerHTML=this.suggestionMarkup();return;}
      if(action==='copy-suggestion'){const s=this.response.suggestions.find(x=>x.id===button.dataset.suggestion);await navigator.clipboard.writeText(s.data.title+'\n\n'+s.data.paragraphs.map(p=>p.text).join('\n\n'));toast('已复制建议，可选择合适内容合并');return;}
      if(action==='adopt'){await this.flush();await this.request('/api/articles/'+this.id+'/adopt-suggestion',{base_revision:this.revision,suggestion_id:button.dataset.suggestion});await editor(this.id);toast('已采用建议，原版本保留在历史中');return;}
      if(action==='restore'){const recovery=this.recovery;this.recoveredKey=recovery.key;this.mutate(()=>{this.doc=editorClone(recovery.document);});$('#document-title').value=this.doc.title;this.autoHeight();$('#document-alert').hidden=true;if(recovery.revision!==this.revision){this.blocked=true;this.saveError='本地草稿与服务器版本不同';this.alert('恢复的草稿基于较早版本。请比较后决定保存方式。','conflict');}return;}
      if(action==='discard-recovery'){localStorage.removeItem(this.recovery.key);$('#document-alert').hidden=true;return;}
      if(action==='compare'){const r=await this.request('/api/articles/'+this.id);this.drawer('服务器版本 v'+r.article.revision,`<p class="notice">左侧仍是你的编辑。对比后可手动合并，或选择“以当前编辑为准保存”。</p><h3>${esc(r.document.title)}</h3>${r.document.blocks.map(b=>b.type==='text'?`<p>${esc(b.text)}</p>`:`<img class="material-detail-image" src="${esc(this.photo(b.asset_id)?.preview_url)}" alt="服务器图片">`).join('')}`);return;}
      if(action==='keep-current'){const r=await this.request('/api/articles/'+this.id);this.revision=r.article.revision;this.blocked=false;await this.flush();$('#document-alert').hidden=true;return;}
      if(action==='backup'){const url=URL.createObjectURL(new Blob([JSON.stringify({article_id:this.id,revision:this.revision,document:this.doc},null,2)],{type:'application/json'})),link=document.createElement('a');link.href=url;link.download='本地图文草稿-'+this.id.slice(0,8)+'.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),10000);return;}
    }catch(error){this.alert(error.message,error.code==='revision_conflict'?'conflict':this.blocked?'save':'notice');button.disabled=false;}
  }
  async leave() {
    clearTimeout(this.timer);clearTimeout(this.runTimer);
    this.resizeObserver?.disconnect();
    document.body.classList.remove('document-editing');this.root.classList.remove('document-full-preview');this.root.removeAttribute('data-view');
    this.root.onclick=this.root.oninput=this.root.onsubmit=this.root.onfocusin=this.root.oncompositionstart=this.root.oncompositionend=this.root.ondragstart=this.root.ondragover=this.root.ondragleave=this.root.ondrop=null;
    this.root.onpointerdown=this.root.onpointermove=this.root.onpointerup=this.root.onpointercancel=null;
    activeDocumentEditor=null;currentArticle=null;history.replaceState({},'','/');page='articles';await refresh();
  }
}

editor = async function(id) {
  if(activeDocumentEditor){await activeDocumentEditor.flush();clearTimeout(activeDocumentEditor.runTimer);activeDocumentEditor.resizeObserver?.disconnect();}
  const response=await api('/api/articles/'+id);
  activeDocumentEditor=new DocumentEditor(response);activeDocumentEditor.mount();
};
window.addEventListener('beforeunload',e=>{const ed=activeDocumentEditor;if(ed&&(ed.savedSequence!==ed.sequence||ed.blocked)){ed.persistLocal();e.preventDefault();e.returnValue='';}});
window.addEventListener('online',()=>{const ed=activeDocumentEditor;if(ed?.blocked&&!ed.saveError?.includes('窗口')&&!ed.saveError?.includes('版本')){ed.blocked=false;ed.flush().catch(()=>{});}});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&activeDocumentEditor){$('#main').classList.remove('document-full-preview');if($('#document-drawer'))$('#document-drawer').hidden=true;}});
