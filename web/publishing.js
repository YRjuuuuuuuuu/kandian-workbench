'use strict';
// This tracks the user's local workflow. It never submits anything to Toutiao.
let publicationFilter='all';
const Publishing={
  labels:{draft:'草稿',pending:'已导出 · 待发布',published:'已发布'},
  badge(summary={state:'draft'},revision) {
    const state=summary.state||'draft',record=summary.publication;
    const label=this.labels[state]+(record?.revision?' v'+record.revision:'');
    return `<span class="tag ${state==='published'?'green':state==='pending'?'amber':'blue'}">${esc(label)}</span>${summary.has_local_changes?`<span class="tag amber">本地 v${revision} 有更新</span>`:''}`;
  },
  progress(summary={state:'draft'}) {
    const record=summary.publication,last=summary.latest_export;
    if(record)return `确认于 ${time(record.confirmed_at)}${record.published_at?' · 发布于 '+time(record.published_at):''}`;
    return last?`导出 v${last.revision} · ${time(last.created_at||last.delivered_at)}${!summary.export_is_current?' · 当前有未导出修改':''}`:'尚未记录导出或发布';
  },
  button(id,summary={state:'draft'},cls='small') {
    return `<button class="button ${cls}" data-publication-action="open" data-article="${esc(id)}" ${summary.is_demo?'disabled':''}>${summary.is_demo?'演示稿':summary.publication?'发布记录':'标记已发布'}</button>`;
  },
  strip(id,summary,revision) {
    return `<div class="publication-strip"><div><div class="meta">${this.badge(summary,revision)}</div><small>${esc(this.progress(summary))}</small></div><div class="row-actions">${summary?.publication?.remote_url?`<a class="button ghost small" href="${esc(summary.publication.remote_url)}" target="_blank" rel="noopener noreferrer">查看作品 ↗</a>`:''}${this.button(id,summary)}</div></div>`;
  },
  apply(id,summary) {
    const article=S?.articles.find(a=>a.id===id);if(article)article.publication=summary;
    if(typeof activeDocumentEditor!=='undefined'&&activeDocumentEditor?.id===id)activeDocumentEditor.updatePublication(summary);
    if(page==='articles')render();
  },
  pendingKey(id,receipt) {return 'ce-export-receipt:'+id+':'+receipt;},
  async download(id,revision,zip=false,skipMissing=false) {
    const response=await fetch('/api/articles/'+id+'/export-download',{method:'POST',headers:{'X-App-Token':token,'Content-Type':'application/json'},body:JSON.stringify({revision,format:zip?'zip':'docx',skip_missing:skipMissing})});
    if(!response.ok){const error=await response.json();throw new Error(error.error||'导出失败');}
    const receipt=response.headers.get('X-Export-Id'),blob=await response.blob();
    if(!receipt)throw new Error('未取得导出回执，请刷新工作台后重试');
    const url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download=`${zip?'图文包':'头条图文'}-${id.slice(0,8)}.${zip?'zip':'docx'}`;link.click();setTimeout(()=>URL.revokeObjectURL(url),10000);
    try{sessionStorage.setItem(this.pendingKey(id,receipt),JSON.stringify({id,receipt}));}catch{}
    try{
      const summary=await api('/api/articles/'+id+'/export-complete',{receipt});
      try{sessionStorage.removeItem(this.pendingKey(id,receipt));}catch{}
      this.apply(id,summary);return {recorded:true,summary};
    }catch{return {recorded:false,receipt};}
  },
  async retryReceipts() {
    let rows=[];
    try{rows=Object.keys(sessionStorage).filter(k=>k.startsWith('ce-export-receipt:')).flatMap(key=>{try{return [{key,...JSON.parse(sessionStorage.getItem(key))}];}catch{return [];}});}catch{}
    let failed=0;
    for(const row of rows){
      try{const summary=await api('/api/articles/'+row.id+'/export-complete',{receipt:row.receipt});sessionStorage.removeItem(row.key);this.apply(row.id,summary);}catch{failed++;}
    }
    return failed;
  },
  async open(id) {
    const detail=await api('/api/articles/'+id+'/publication');this.current=detail;
    const active=detail.publication,options=[],seen=new Set();
    if(active){options.push({value:'published',revision:active.revision,title:active.title,label:`已发布 ${active.revision?'v'+active.revision:'版本未记录'}（保留当前发布版本）`});seen.add(active.document_hash);}
    for(const row of detail.exports){if(seen.has(row.document_hash))continue;seen.add(row.document_hash);options.push({value:'export:'+row.id,revision:row.revision,title:row.title,label:`已导出 v${row.revision} · ${time(row.created_at||row.delivered_at)}${row.format==='zip'?' · 图文包':''}`});}
    options.push({value:'current',revision:detail.revision,title:detail.title,label:`当前已保存 v${detail.revision}（可用于直接复制后发布）`});
    this.options=options;
    const target=active?'published':detail.latest_export?'export:'+detail.latest_export.id:'current';
    openModal(active?'发布记录':'确认文章已发布',`<form id="publication-form" data-article="${id}"><p class="publication-document-title">${esc(detail.title)}</p><p class="small muted">账号：${esc(detail.account.name)} · 由你手动确认，本地记录不会替你向头条发布</p><label class="field section-gap"><span>发布版本</span><select name="target" id="publication-target">${options.map(o=>`<option value="${o.value}" ${o.value===target?'selected':''}>${esc(o.label)}</option>`).join('')}</select></label><div id="publication-version-note" class="notice publication-note"></div>${active?`<p class="small muted">${esc(this.progress(detail))}${active.source==='legacy'?' · 沿用历史已发布记录':''}</p>`:''}<details class="section-gap" ${active?.remote_url||active?.published_at?'open':''}><summary>补充信息（可选）</summary><div class="form-grid section-gap">${field('头条作品链接','remote_url',active?.remote_url||'','url','placeholder="https://www.toutiao.com/article/…"')}${field('实际发布时间 · 北京时间','published_at',active?.published_at?localInput(active.published_at):'','datetime-local')}<p class="small muted full">不清楚实际发布时间可以留空；确认时间会自动记录。</p></div></details><div id="publication-form-error" class="notice red" role="alert" hidden></div><div class="form-actions"><button class="button" type="button" data-publication-action="cancel">取消</button><button class="button primary" type="submit">${active?'保存发布记录':'确认已发布'}</button></div>${active?'<div class="publication-revoke"><button class="button ghost danger small" type="button" data-publication-action="ask-revoke">撤销发布标记</button><div id="publication-revoke-confirm" class="notice" hidden><p>仅撤销本地标记，头条上的作品仍保留。确认撤销？</p><button class="button small" type="button" data-publication-action="cancel-revoke">保留标记</button><button class="button danger small" type="button" data-publication-action="revoke">确认撤销本地标记</button></div></div>':''}</form>${this.historyMarkup(detail)}`);
    const eyebrow=$('#modal-eyebrow'),old=eyebrow.textContent;eyebrow.textContent='发布记录 · 你自行确认';$('#modal').addEventListener('close',()=>{eyebrow.textContent=old;},{once:true});
    this.versionNote();
  },
  versionNote() {
    const selected=this.options?.find(o=>o.value===$('#publication-target')?.value),detail=this.current;if(!selected||!detail)return;
    const note=$('#publication-version-note');
    note.textContent=selected.revision!==detail.revision?`本次记录发布的是 ${selected.revision?'v'+selected.revision:'历史版本'}，当前已保存为 v${detail.revision}。本地较新修改不会自动视为已发布。`:'确认后记录这个版本已发布；正文仍可编辑和导出。';
    if(selected.title&&selected.title!==detail.title)note.textContent+=' 所选发布稿标题：'+selected.title;
    if(typeof activeDocumentEditor!=='undefined'&&activeDocumentEditor?.id===detail.article_id&&activeDocumentEditor.sequence!==activeDocumentEditor.savedSequence)note.textContent+=' 当前还有未保存编辑，不会计入本次记录。';
  },
  historyMarkup(detail) {
    const events=detail.events||[];
    if(!events.length&&!detail.history.length)return '';
    return `<details class="section-gap publication-history"><summary>操作历史</summary>${events.map(e=>`<p><span>${esc(time(e.created_at))}</span> ${esc({'publication.confirm':'确认已发布','publication.update':'更新发布记录','publication.revoke':'撤销本地发布标记'}[e.action]||e.action)}${e.detail.target_revision?' · v'+e.detail.target_revision:''}</p>`).join('')}${detail.history.filter(r=>r.state!=='active').map(r=>`<p>${esc(r.revision?'v'+r.revision:'历史版本')} · ${r.state==='revoked'?'标记已撤销':'已更新发布版本'} · ${esc(time(r.updated_at))}</p>`).join('')}</details>`;
  },
  async submit(form,action) {
    const detail=this.current,active=detail.publication,values=Object.fromEntries(new FormData(form)),target=values.target;
    const body={action:action|| (active?'update':'confirm'),expected_id:active?.id||null,expected_version:active?.version||0,remote_url:values.remote_url||'',published_at:values.published_at||null};
    if(target==='published')body.target='published';else if(target.startsWith('export:'))body.export_id=target.slice(7);else body.revision=detail.revision;
    const summary=await api('/api/articles/'+detail.article_id+'/publication',body);
    this.apply(detail.article_id,summary);$('#modal').close();
    toast(body.action==='revoke'?'已撤销本地标记，历史记录保留':active?'发布记录已更新':'已标记为已发布');
  }
};

document.addEventListener('click',async e=>{
  const filter=e.target.closest('[data-publication-filter]');if(filter){publicationFilter=filter.dataset.publicationFilter;render();return;}
  const button=e.target.closest('[data-publication-action]');if(!button)return;
  e.preventDefault();const action=button.dataset.publicationAction;
  try{
    if(action==='open'){button.disabled=true;await Publishing.open(button.dataset.article);}
    if(action==='cancel')$('#modal').close();
    if(action==='ask-revoke')$('#publication-revoke-confirm').hidden=false;
    if(action==='cancel-revoke')$('#publication-revoke-confirm').hidden=true;
    if(action==='revoke'){button.disabled=true;await Publishing.submit($('#publication-form'),'revoke');}
  }catch(error){const inline=$('#publication-form-error');if(inline){inline.hidden=false;inline.textContent=error.message;}else toast(error.message,true);}
  finally{if(button.isConnected)button.disabled=false;}
});
document.addEventListener('change',e=>{if(e.target.id==='publication-target')Publishing.versionNote();});
document.addEventListener('submit',async e=>{
  if(e.target.id!=='publication-form')return;e.preventDefault();
  const form=e.target,button=$('[type=submit]',form),error=$('#publication-form-error');button.disabled=true;error.hidden=true;
  try{await Publishing.submit(form);}catch(err){error.hidden=false;error.textContent=err.message;}finally{if(button.isConnected)button.disabled=false;}
});
window.addEventListener('online',()=>Publishing.retryReceipts().catch(()=>{}));
window.addEventListener('load',()=>Publishing.retryReceipts().catch(()=>{}));
