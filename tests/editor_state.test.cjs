// Isolated controller tests: no live browser, platform or production database.
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

function editor() {
  const elements = {status:{},root:{classList:{add(){},remove(){}}}};
  const context = {crypto,console,setTimeout,clearTimeout,URL,Blob,token:'test',window:{addEventListener(){}},document:{addEventListener(){}},editor:null,
    $:()=>null,$$:()=>[],esc:String,localStorage:{setItem(){},removeItem(){}},toast(){}};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/editor.js'),'utf8')+'\nthis.Editor = DocumentEditor;',context);
  const ed = new context.Editor({article:{id:'synthetic',revision:1},document:{version:1,title:'初稿',blocks:[{id:'one',type:'text',text:'正文'},{id:'image1',type:'image',asset_id:'p1',caption:''},{id:'image2',type:'image',asset_id:'p2',caption:''}],cover:{mode:'auto',asset_id:null}},photos:[]});
  ed.setStatus=()=>{};ed.persistLocal=()=>{};ed.updatePreview=()=>{};ed.alert=()=>{};ed.drawBlocks=()=>{};ed.queueSave=()=>{};
  return ed;
}
const tick = () => new Promise(resolve=>setImmediate(resolve));
test('preview keeps the same single file-photo label as Word',()=>{const ed=editor();ed.response.photos=[{id:'p1',data:{photo_role:'file_photo'},credit_suffix:''}];assert.equal(ed.caption({asset_id:'p1',caption:'球员（资料照）'}),'球员（资料照）');});
function deferred() { let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject}; }

test('rapid consecutive edits serialize requests and flush the latest layout',async()=>{
  const ed=editor(),first=deferred(),second=deferred(),calls=[];
  ed.request=async(url,body)=>{calls.push(structuredClone(body));return calls.length===1?first.promise:second.promise;};
  ed.doc.title='第一次修改';ed.sequence=1;
  const saving=ed.flush();
  ed.doc.title='最终修改';ed.doc.blocks.reverse();ed.sequence=2;
  const flushAgain=ed.flush();
  assert.equal(calls.length,1);
  first.resolve({revision:2,document:calls[0].document});await tick();
  assert.equal(calls.length,2);assert.equal(calls[1].base_revision,2);assert.equal(calls[1].document.title,'最终修改');assert.equal(calls[1].document.blocks[0].asset_id,'p2');
  second.resolve({revision:3,document:calls[1].document});await Promise.all([saving,flushAgain]);
  assert.equal(ed.savedSequence,2);assert.equal(ed.revision,3);assert.equal(ed.doc.title,'最终修改');
});

test('network failure retains unsaved work and retry uses the same base revision',async()=>{
  const ed=editor();ed.sequence=1;ed.doc.title='网络中断期间的手工编辑';
  ed.request=async()=>{throw new Error('offline');};
  await assert.rejects(ed.flush(),/offline/);assert.equal(ed.doc.title,'网络中断期间的手工编辑');assert.equal(ed.revision,1);assert.equal(ed.blocked,true);
  ed.request=async(url,body)=>({revision:2,document:body.document});ed.blocked=false;await ed.flush();assert.equal(ed.savedSequence,1);assert.equal(ed.doc.title,'网络中断期间的手工编辑');
});

test('409 conflict does not replace the human draft or advance its base revision',async()=>{
  const ed=editor();ed.sequence=1;ed.doc.title='保留我的编辑';
  ed.request=async()=>{const e=new Error('version conflict');e.code='revision_conflict';throw e;};
  await assert.rejects(ed.flush(),/conflict/);assert.equal(ed.doc.title,'保留我的编辑');assert.equal(ed.revision,1);assert.equal(ed.savedSequence,0);
});

test('IME composition never sends an intermediate document, including during an in-flight save',async()=>{
  const ed=editor(),first=deferred();let calls=0;ed.sequence=1;
  ed.request=async()=>{calls++;return first.promise;};const saving=ed.flush();
  ed.composing=true;ed.doc.title='zhong正在组词';ed.sequence=2;
  first.resolve({revision:2,document:{...ed.doc,title:'已完成的旧文本'}});await saving;
  assert.equal(calls,1);assert.equal(ed.doc.title,'zhong正在组词');assert.equal(ed.savedSequence,1);
  await assert.rejects(ed.flush(),/输入法/);
  ed.composing=false;ed.doc.title='中文输入已完成';ed.request=async(url,body)=>({revision:3,document:body.document});await ed.flush();assert.equal(ed.savedSequence,2);
});

test('automatic cover follows first image and manual cover survives reorder',()=>{
  const ed=editor();assert.equal(ed.coverId(),'p1');ed.move('image2',0);assert.equal(ed.coverId(),'p2');
  ed.doc.cover={mode:'manual',asset_id:'p1'};ed.move('image2',2);assert.equal(ed.coverId(),'p1');
});
