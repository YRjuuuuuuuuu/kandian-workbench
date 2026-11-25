const test=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
function setup(){
 const ai={production_provider:'deepseek',deepseek_model:'deepseek-flash',deepseek_max_tokens:4096,codex_model:'gpt-6.1-terra',research_model:'gpt-6.1-terra',reasoning_effort:'medium',research_reasoning_effort:'high',workbuddy_model:'由 WorkBuddy 内选择'};
 const ctx={document:{addEventListener(){}},A:{runtime:{}},esc:v=>String(v??''),aiSelection:()=>ai,FormData:class{constructor(form){return Object.entries(form.fields);}},aiOptions:(values,selected)=>[...new Set([...values,selected])].map(v=>`<option>${v}</option>`).join('')};
 vm.createContext(ctx);vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/models.js'),'utf8'),ctx);return {ctx,ai};
}
test('legacy mixed configuration is displayed honestly and full DeepSeek shows every stage',()=>{
 const {ctx,ai}=setup();assert.equal(ctx.aiRoutes(ai).research.provider,'codex');
 assert.match(ctx.aiRunLabel({kind:'discover',ai}),/Codex/);
 assert.match(ctx.aiRunLabel({kind:'produce',ai}),/查证 Codex/);
 ai.stage_routes=Object.fromEntries(Object.keys(ctx.aiRoutes(ai)).map(k=>[k,{provider:'deepseek',model:'deepseek-flash',effort:'medium',max_tokens:4096}]));
 const html=ctx.aiFields(ai);assert.equal((html.match(/data-stage="/g)||[]).length,7);assert.match(html,/全链路 DeepSeek/);assert.doesNotMatch(ctx.aiRouteLines(ai),/gpt-6/);assert.match(html,/Search \/ Fetch/);
});
test('saving independent choices retains all seven routes and writing legacy aliases',()=>{
 const {ctx,ai}=setup();const fields={};
 for(const k of Object.keys(ctx.aiRoutes(ai))){fields[`ai_${k}_provider`]='deepseek';fields[`ai_${k}_model`]='deepseek-flash';fields[`ai_${k}_effort`]='medium';fields[`ai_${k}_tokens`]='4096';}
 fields.ai_research_provider='codex';fields.ai_research_model='gpt-6.1-sol';fields.ai_research_effort='low';
 const saved=ctx.readAI({fields});assert.equal(saved.stage_routes.research.model,'gpt-6.1-sol');assert.equal(saved.stage_routes.research.effort,'low');assert.equal(saved.stage_routes.writing.provider,'deepseek');assert.equal(saved.production_provider,'deepseek');assert.equal(saved.stage_routes.polish.max_tokens,4096);
 fields.ai_revision_provider='workbuddy';fields.ai_revision_workbuddy='桌面自选模型';assert.equal(ctx.readAI({fields}).stage_routes.revision.model,'桌面自选模型');
});

test('legacy WorkBuddy file handoff does not claim to execute visual review',()=>{
 const {ctx,ai}=setup();ai.stage_routes={writing:{provider:'workbuddy',model:'桌面自选模型'},polish:{provider:'workbuddy',model:'桌面自选模型'},research:{provider:'codex',model:'gpt-6.1-terra'}};
 assert.equal(ctx.aiRoutes(ai).image_review.provider,'codex');assert.equal(ctx.aiRoutes(ai).style_learning.provider,'codex');
});
test('task summary shows actual returned model separately from planned choice',()=>{
 const {ctx,ai}=setup();const html=ctx.runAIConfig({kind:'produce',ai,ai_routes_explicit:true},[{kind:'provider',data:{stage_key:'writing',requested_model:'deepseek-flash',reported_model:'deepseek-flash-returned'}}]);
 assert.match(html,/实际返回：deepseek-flash-returned/);assert.match(html,/Codex · gpt-6.1-terra/);assert.match(html,/只影响新任务/);
});
test('illustration route reports tools rather than suggesting a hidden model call',()=>{
 const {ctx,ai}=setup();assert.match(ctx.runAIConfig({kind:'illustrate',ai},[]),/配图规划与核对/);
});

test('the independent build presents DeepSeek as the only runnable provider',()=>{
 const {ctx,ai}=setup();
 ai.stage_routes=Object.fromEntries(Object.keys(ctx.aiRoutes(ai)).map(stage=>[
  stage,{provider:'deepseek',model:'deepseek-flash',effort:'medium',max_tokens:4096}
 ]));
 ctx.A.runtime.models={deepseek_only:true};
 const html=ctx.aiFields(ai);
 assert.equal((html.match(/value="deepseek"/g)||[]).length,7);
 assert.doesNotMatch(html,/value="codex"|value="workbuddy"|全链路 Codex/);
});
