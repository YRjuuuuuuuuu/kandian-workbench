const test=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');

function setup(){
 const storage={setItem(k,v){this[k]=v;},getItem(k){return this[k]||null;},removeItem(k){delete this[k];}};
 const clicks=[],calls=[],nodes={
  '#modal-eyebrow':{textContent:'编辑',},'#modal':{addEventListener(){},close(){this.closed=true;}},
  '#publication-target':{value:'export:e6'},'#publication-version-note':{textContent:''}
 };
 const context={token:'local-test-token',S:{articles:[]},page:'editor',activeDocumentEditor:null,
  document:{addEventListener(){},createElement(){const a={click(){clicks.push({name:a.download,url:a.href});}};return a;}},
  window:{addEventListener(){}},sessionStorage:storage,URL:{createObjectURL(){return 'blob:local-test';},revokeObjectURL(){}},setTimeout(){},
  $:selector=>nodes[selector],esc:x=>String(x??''),time:x=>x||'',localInput:x=>x,field:()=>'',toast(){},
  FormData:class{constructor(form){return Object.entries(form.fields);}},
  api:async(url,body)=>{calls.push({url,body});return {state:'pending'};},
  fetch:async()=>({ok:true,headers:{get:()=> 'receipt-one'},blob:async()=>({})}),
  openModal(title,html){context.modal={title,html};}
 };
 vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/publishing.js'),'utf8')+'\nthis.Pub=Publishing;',context);
 return {context,pub:context.Pub,storage,clicks,calls,nodes};
}

test('successful download acknowledges its receipt after file delivery',async()=>{
 const {pub,context,clicks,calls,storage}=setup();let body;
 context.fetch=async(url,options)=>{body=JSON.parse(options.body);return {ok:true,headers:{get:()=> 'receipt-one'},blob:async()=>({})};};
 const result=await pub.download('article',6);
 assert.equal(result.recorded,true);assert.equal(body.revision,6);assert.equal(body.format,'docx');
 assert.equal(clicks.length,1);assert.equal(calls[0].url,'/api/articles/article/export-complete');assert.equal(calls[0].body.receipt,'receipt-one');
 assert.equal(Object.keys(storage).filter(k=>k.startsWith('ce-export-receipt:')).length,0);
});

test('failed response body does not acknowledge export or claim a delivered file',async()=>{
 const {pub,context,clicks,calls}=setup();context.fetch=async()=>({ok:true,headers:{get:()=> 'receipt-one'},blob:async()=>{throw new Error('network interrupted');}});
 await assert.rejects(pub.download('article',6),/interrupted/);assert.equal(clicks.length,0);assert.equal(calls.length,0);
});

test('receipt outage is retryable without downloading the file again',async()=>{
 const {pub,context,clicks,calls}=setup();context.api=async()=>{throw new Error('offline');};
 const result=await pub.download('article',6);assert.equal(result.recorded,false);assert.equal(clicks.length,1);
 context.api=async(url,body)=>{calls.push({url,body});return {state:'pending'};};
 assert.equal(await pub.retryReceipts(),0);assert.equal(clicks.length,1);assert.equal(calls.length,1);
});

test('confirmation selects the exported revision rather than newer editing content',async()=>{
 const {pub,calls}=setup();pub.current={article_id:'article',revision:7,publication:null};
 await pub.submit({fields:{target:'export:e6',remote_url:'',published_at:''}});
 assert.equal(calls[0].body.export_id,'e6');assert.equal(calls[0].body.revision,undefined);assert.equal(calls[0].body.published_at,null);
 assert.equal(calls[0].body.expected_id,null);
});

test('direct-copy publication can confirm current revision with no mandatory details',async()=>{
 const {pub,calls}=setup();pub.current={article_id:'article',revision:7,publication:null};
 await pub.submit({fields:{target:'current',remote_url:'',published_at:''}});
 assert.equal(calls[0].body.revision,7);assert.equal(calls[0].body.action,'confirm');
});

test('version warning and published badges retain explicit old/current revision distinction',()=>{
 const {pub,nodes}=setup();pub.current={article_id:'article',revision:7};pub.options=[{value:'export:e6',revision:6}];pub.versionNote();
 assert.match(nodes['#publication-version-note'].textContent,/v6/);assert.match(nodes['#publication-version-note'].textContent,/v7/);
 const badge=pub.badge({state:'published',publication:{revision:6},has_local_changes:true},7);
 assert.match(badge,/已发布 v6/);assert.match(badge,/本地 v7 有更新/);
});
