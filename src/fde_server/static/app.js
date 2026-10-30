'use strict';
const state = {session:null, page:'workspaces', workspaces:[], current:null, tab:'report', draft:'', kbs:[], selectedKb:null, deleted:false, authMode:'login'};
const app = document.querySelector('#app');
const dialog = document.querySelector('#dialog');
const esc = x => String(x ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels = {summarize:'业务概要',interview:'访谈追问',recommend:'业务推荐',match:'数据对齐',report:'业务理解',revise:'结果修订',refine:'业务细化',learn:'反馈回传',process:'流程',system:'系统',question:'问题',ontology:'本体',binding:'数据绑定',gap:'数据缺口',summary:'概要',challenge_questions:'挑战与目标',questions:'问题',text:'内容',name:'名称',statement:'业务定义',description:'说明',steps:'步骤',roles:'角色',object:'业务内容',confidence:'置信度',evidence:'依据',why:'原因',why_valuable:'业务价值',asked_by:'使用者',data_needs:'所需数据',object_types:'业务对象',link_types:'业务关系',action_types:'业务动作',business_problems:'业务问题',processes:'业务流程',systems_to_build:'系统建议',agents_to_build:'Agent 建议',key_properties:'关键属性',backed_by:'对应表',from:'起点',to:'终点',cardinality:'关系基数',overview:'业务概览',report:'业务理解',changes:'建议修改',before:'修改前',after:'修改后',understood:'修改意图',suggestions:'细化建议',fields:'字段',scope:'范围',grain:'粒度',stop:'访谈状态',reason:'说明',options:'可选回答',business_model:'业务模式',customers:'客户',products_services:'产品与服务',value_chain:'价值链',known_facts:'已知事实',unknowns:'待确认',contradictions:'需核对',type:'类型',target:'修改位置',intent:'意图',role:'影响类型',round:'访谈轮次',should_stop:'是否结束访谈',hypothesis_version:'业务假设版本',coverage:'覆盖率',gate:'对齐结论',answerability:'可回答性',missing:'缺少的数据',systems:'系统',node_summary:'业务支撑',unclaimed:'待解释资产',items:'明细',content:'内容'};
Object.assign(labels,{one_liner:'一句话概要',industry:'行业',offerings:'产品与服务',what:'具体内容',who:'业务对象',revenue_model:'收入模式',how:'运作方式',scale:'经营规模',note:'说明',business_lines:'业务条线',stage:'业务环节',systems_mentioned:'已使用系统',open_points:'待确认事项',dimension:'关注维度',semantic:'业务概念',asset:'数据资产',score:'匹配度',action:'建议操作',on:'所属对象',kind:'类型',sources:'数据源',must:'必需信息',temporal:'时间覆盖',reason_code:'原因',undetermined_reasons:'待确认原因',structural_coverage:'结构覆盖',plan:'回答计划',fingerprint:'数据需求',matched:'匹配结果'});
const valueLabels={fake:'系统生成',challenge:'业务挑战',problem:'当前问题',goal:'期望目标',ask:'追问',direct:'直接修改',ripple:'连带影响',bind:'绑定字段',offline:'线下步骤',gap:'数据缺口',answerable:'可以回答',partial:'部分可回答',not_answerable:'暂不可回答',undetermined:'待确认',not_scanned:'尚未扫描',absent_in_scanned_scope:'已扫描范围内未发现',matched:'已匹配',unknown:'暂不确定'};
const phases = ['intro','challenge','data','interview','report','refine'];
const phaseNames = ['业务概要','挑战与需求','接入资料','访谈与对齐','理解与本体','业务细化'];
const statuses = {queued:'等待处理',running:'处理中',retry_wait:'等待重试',succeeded:'已完成',failed:'失败',proposed:'待确认',confirmed:'已确认',rejected:'已驳回',superseded:'旧版本',ready:'可检索',processing:'索引中',purged:'已清除',settled:'已结算',reserved:'已预占',settlement_pending:'待结算',execution_unknown:'执行待确认'};
const errors = {PIPELINE_CHANGED:'任务基于旧版流程，请重新发起任务',QUOTA_EXHAUSTED:'组织模型额度不足，请到 Router 充值',WORKSPACE_CHANGED:'资料已变化，请重新发起生成',INPUT_SENSITIVE:'资料含敏感信息，请脱敏后重试',SOURCE_REVOKED:'引用资料已撤销或更新',ACCESS_REVOKED:'访问权限已撤销',ROUTER_UNAVAILABLE:'模型网关暂时不可用',PATTERN_UNAVAILABLE:'业务服务暂时不可用',CONTEXT_TOO_LARGE:'资料超过任务上下文限制',CAPABILITY_BUDGET_EXCEEDED:'本次任务达到预算上限',MODEL_NOT_ALLOWED:'所选模型未获授权',ROUTER_NOT_BOUND:'请先绑定组织模型账户'};
Object.assign(errors,{PROVIDER_TIMEOUT:'模型生成超时，可手动重试或更换模型',PROVIDER_AUTH_FAILED:'模型密钥无效或没有此模型的权限',PROVIDER_REQUEST_FAILED:'模型服务拒绝请求，请检查模型与协议',PROVIDER_RESPONSE_INVALID:'模型没有返回有效的业务 JSON，请手动重试或更换模型',PROVIDER_OUTPUT_LIMIT:'模型输出达到长度上限，请缩小分析范围',MODEL_CAPABILITY_UNSUPPORTED:'请选择对话模型；图像、语音或向量模型不能用于业务分析',MODEL_NOT_CONFIGURED:'请在我的模型配置对话模型，或绑定组织模型账户',PROVIDER_NOT_FOUND:'个人提供商已停用或无权访问'});
const roleNames = {tenant_owner:'组织所有者',tenant_admin:'组织管理员',fde_member:'FDE 成员',billing_viewer:'账务查看者',workspace_editor:'工作区编辑者',workspace_viewer:'工作区查看者'};
const admin = () => ['tenant_owner','tenant_admin'].includes(state.session?.role);
function csrf(){return decodeURIComponent(document.cookie.split('; ').find(c=>c.startsWith('fde_csrf='))?.split('=').slice(1).join('=') || '');}
async function api(path, options={}){
  const headers={'X-CSRF-Token':csrf(),...(options.headers||{})};
  if(options.body && !(options.body instanceof FormData)){headers['Content-Type']='application/json';options.body=JSON.stringify(options.body);}
  const res=await fetch('/api'+path,{credentials:'same-origin',...options,headers});
  const data=await res.json();
  if(!res.ok){const error=new Error(data.error?.message || '请求失败');error.code=data.error?.code;error.status=res.status;throw error;}
  return data;
}
function toast(message){const el=document.querySelector('#toast');el.textContent=message;el.style.display='block';clearTimeout(toast.timer);toast.timer=setTimeout(()=>el.style.display='none',5500);}
function date(x){return x?new Date(x).toLocaleString('zh-CN',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}):'—';}
function badge(value){return `<span class="pill ${['failed','rejected'].includes(value)?'bad':['queued','proposed','retry_wait','settlement_pending'].includes(value)?'warn':''}">${esc(statuses[value]||value)}</span>`;}
function btn(text,action,attrs='',cls=''){return `<button type="button" class="${cls}" data-action="${action}" ${attrs}>${text}</button>`;}
function empty(title,text){return `<div class="empty"><strong>${esc(title)}</strong>${esc(text)}</div>`;}
function field(label,name,value='',type='text',extra=''){return `<label for="f-${name}">${label}</label><input id="f-${name}" name="${name}" type="${type}" value="${esc(value)}" ${extra}>`;}
function area(label,name,value=''){return `<label for="f-${name}">${label}</label><textarea id="f-${name}" name="${name}" rows="4">${esc(value)}</textarea>`;}
function modal(title,body,kind,attrs='',submitText='确定'){if(dialog.open)dialog.close();document.querySelector('#dialog-content').innerHTML=`<h2>${esc(title)}</h2><form data-kind="${kind}" ${attrs}>${body}<div class="error-slot"></div><div class="actions">${btn('取消','close')}<button class="primary" type="submit">${submitText}</button></div></form>`;dialog.showModal();}
function details(value, depth=0){
  if(value===null || value===undefined) return '';
  if(Array.isArray(value)) return `<div class="detail-list">${value.map(v=>`<div class="${typeof v==='object'?'nested':'detail'}">${details(v,depth+1)}</div>`).join('')}</div>`;
  if(typeof value==='object') return Object.entries(value).filter(([k])=>!['artifact_id','id','produced_by','snapshot_id','labels_ref','versions','version','evidence','evidence_id','for','pipeline','temp_id','resolves','allow_other','kind','source_id','target_id','slot_id','dropped_invalid'].includes(k)).map(([k,v])=>`<div class="detail"><div class="detail-title">${esc(labels[k]||k)}</div><div class="detail-value">${details(v,depth+1)}</div></div>`).join('');
  return esc(valueLabels[value]||value);
}
function shell(content){
  const me=state.session, tenant=me.tenants.find(t=>t.id===me.tenant_id);
  const nav=[['workspaces','◫','工作区'],['knowledge','▤','专业知识库'],['providers','◇','我的模型'],...(admin()?[['members','♧','组织成员'],['organization','⚙','组织设置']]:[]),...((admin()||me.role==='billing_viewer')?[['billing','◉','模型与用量']]:[]),...(admin()?[['audit','≋','审计记录']]:[]),...(me.platform_admin?[['admin','▦','平台管理']]:[])];
  app.innerHTML=`<div class="layout"><aside class="sidebar"><div class="brand"><i>S</i>Sudo <span class="muted">FDE</span></div><div class="eyebrow">当前组织</div><select class="tenant-switch" id="tenant-switch" aria-label="切换组织">${me.tenants.map(t=>`<option value="${esc(t.id)}" ${t.id===me.tenant_id?'selected':''}>${esc(t.name)}</option>`).join('')}</select><nav class="nav">${nav.map(([id,icon,label])=>`<button data-action="nav" data-page="${id}" class="${state.page===id?'active':''}"><span>${icon}</span>${label}</button>`).join('')}</nav></aside><div class="shell"><header class="topbar"><div class="breadcrumb">${esc(tenant?.name)} <span> / </span> ${state.current&&state.page==='workspaces'?'业务发现':nav.find(n=>n[0]===state.page)?.[2]||'工作台'}</div><div class="top-right"><details class="account-menu"><summary aria-label="账户菜单：${esc(me.user.email)}" title="${esc(me.user.email)}"><span>${esc(me.user.email)}</span></summary><div class="account-dropdown">${btn('退出登录','logout')}</div></details></div></header><main class="content">${content}</main></div></div>`;
}
function renderAuth(){
  const mode=['login','invite','reset','forgot'].includes(state.authMode)?state.authMode:'login';const title={login:'欢迎回来',invite:'加入组织',reset:'设置新密码',forgot:'找回密码'}[mode];
  const hash=new URLSearchParams(location.hash.slice(1));
  let inputs='';
  if(['login','forgot'].includes(mode))inputs+=field('邮箱','email','','email','required autocomplete="email"');
  if(mode==='invite')inputs+=field('你的姓名','name','','text','required');
  if(mode!=='forgot')inputs+=field(mode==='invite'?'密码（已有账号请填原密码）':'密码','password','','password',`required ${mode!=='login'?'minlength="12"':''} autocomplete="${mode==='login'?'current-password':'new-password'}"`);
  if(mode==='invite')inputs+=`<input type="hidden" name="token" value="${esc(hash.get('token'))}"><input type="hidden" name="tenant_id" value="${esc(hash.get('tenant'))}">`;
  if(mode==='reset')inputs+=`<input type="hidden" name="token" value="${esc(hash.get('token'))}">`;
  app.innerHTML=`<div class="login-layout"><div class="login-story"><div class="brand"><i>S</i>Sudo FDE</div><div class="eyebrow">BUSINESS DISCOVERY, TOGETHER</div><h1>从业务的每个细节，<br>找到下一步的答案。</h1><p>把团队的业务知识、专业资料与数据结构连在一起。每一次理解有依据，每一项判断可确认。</p><div class="line"></div><p class="small">组织隔离 · 知识可引用 · 用量可追溯</p></div><div class="login-form"><div class="auth-box"><h2>${title}</h2><p class="muted">${mode==='login'?'登录后继续你的业务发现工作。':'与团队在安全的组织空间中协作。'}</p><form data-kind="auth" data-mode="${mode}">${inputs}<div class="error-slot"></div><button class="primary" type="submit">${mode==='login'?'登录工作台':mode==='forgot'?'发送重置邮件':'继续'}</button></form><div class="auth-links ${mode==='login'?'login-links':''}">${mode!=='login'?btn('返回登录','auth-mode','data-mode="login"'):''}${mode==='login'?btn('忘记密码','auth-mode','data-mode="forgot"'):''}</div></div></div></div>`;
}
async function refreshSession(){state.session=await api('/session');}
function workspaceModelChoices(){
  const session=state.session||{}, choices=[];
  if(session.router_bound){
    (session.chat_models||session.models||[]).forEach((model,index)=>choices.push({provider_id:null,model,label:`组织模型 · ${model}`,preferred:index===0}));
  }
  (session.providers||[]).forEach(provider=>{
    const models=provider.chat_models||provider.models||[];
    const preferred=models.includes(provider.default_chat_model||provider.default_model)?provider.default_chat_model||provider.default_model:models[0];
    models.forEach(model=>choices.push({provider_id:provider.provider_id,model,label:`${provider.display_name} · ${model}`,preferred:model===preferred}));
  });
  return choices.map(choice=>({...choice,key:JSON.stringify([choice.provider_id,choice.model])}));
}
function workspaceModelScope(){return JSON.stringify([state.session?.user.id,state.session?.tenant_id,state.current?.workspace.id]);}
function selectedWorkspaceModel(readControl=true){
  const choices=workspaceModelChoices();
  const scope=workspaceModelScope();
  const selected=readControl?document.querySelector('#model-select')?.value:null;
  state.workspaceModels ||= {};
  const choice=choices.find(item=>item.key===selected)||choices.find(item=>item.key===state.workspaceModels[scope])||choices.find(item=>item.preferred)||choices[0];
  if(choice)state.workspaceModels[scope]=choice.key;
  return choice;
}
function workspaceModelSelector(){
  const choice=selectedWorkspaceModel(false);
  return choice?`<label for="model-select">业务分析模型</label><select id="model-select" aria-label="业务分析模型">${workspaceModelChoices().map(item=>`<option value="${esc(item.key)}" ${item.key===choice.key?'selected':''}>${esc(item.label)}</option>`).join('')}</select>`:'<span class="muted">请在「我的模型」配置对话模型后运行 AI</span>';
}
async function workspaceList(){state.workspaces=await api('/workspaces?deleted='+state.deleted);state.current=null;const rows=state.workspaces;const stats=[['工作区',rows.length,'你的授权项目'],['进行中',rows.filter(x=>!['report','refine'].includes(x.phase)).length,'从资料到业务理解'],['已形成理解',rows.filter(x=>['report','refine'].includes(x.phase)).length,'可继续确认与细化']];
 shell(`<div class="page-head"><div><div class="eyebrow">YOUR BUSINESS WORKSPACES</div><h1>让业务理解，成为团队资产。</h1><p>从一个问题开始，把资料、流程与数据逐步连接起来。</p></div><div class="actions">${btn(state.deleted?'查看工作区':'已归档','toggle-deleted')}${state.session.role!=='billing_viewer'?btn('＋ 新建工作区','new-workspace','','primary'):''}</div></div>${!workspaceModelChoices().length?'<div class="info-box">你可以先保存业务资料。运行 AI 前，请在「我的模型」配置个人对话模型，或由管理员绑定组织模型账户。</div>':''}<div class="stats workspace-stats">${stats.map(([label,v,note])=>`<div class="stat"><div class="label">${label}</div><div class="value">${esc(v)}</div><div class="muted">${note}</div></div>`).join('')}</div><div class="section-label"><span>${state.deleted?'已归档的工作区':'最近的工作区'}</span><span class="muted">默认私有，按需邀请协作</span></div>${rows.length?`<div class="grid">${rows.map(w=>`<article class="card workspace-card" ${state.deleted?'':`data-action="open-workspace" data-id="${w.id}"`} tabindex="0"><div class="actions"><span class="ws-icon">◫</span><span class="spacer"></span><span class="pill">${phaseNames[phases.indexOf(w.phase)]||'业务发现'}</span></div><h3>${esc(w.name)}</h3><div class="bottom"><span>${date(w.created_at)}</span><span>${state.deleted?btn('取消归档','restore',`data-id="${w.id}"`):'进入工作区 ↗'}</span></div></article>`).join('')}</div>`:(state.deleted?empty('暂无已归档的工作区','归档后的工作区会显示在这里，可随时取消归档。'):empty('从第一个工作区开始','新建一个项目，输入公司介绍或上传资料。'))}`);
}
function preferredWorkspaceTab(data){
  const toTab=capability=>capability==='recommend'?'cards':capability;
  const capabilities=['summarize','interview','recommend','match','report','revise','refine'];
  const active=data.jobs.filter(job=>capabilities.includes(job.capability)&&['queued','running','retry_wait'].includes(job.status)).at(-1);
  if(active)return toTab(active.capability);
  const artifact=data.artifacts.filter(row=>row.status!=='superseded'&&row.status!=='rejected'&&capabilities.includes(row.kind)).at(-1);
  return artifact?toTab(artifact.kind):'summarize';
}
async function openWorkspace(id){state.page='workspaces';state.current=await api('/workspaces/'+id);if(state.tab==='auto')state.tab=preferredWorkspaceTab(state.current);renderWorkspace();}
function renderWorkspace(){const v=state.current,w=v.workspace;const workflow=v.workflow||{stages:phases.map((id,i)=>({id,name:phaseNames[i],completed:i<=phases.indexOf(w.phase),active:id===w.phase})),next_action:w.next_action,objective:w.objective};const tabs=[['summarize','概要'],['interview','访谈'],['cards','业务卡片'],['match','对齐'],['report','理解报告'],['revise','修订'],['refine','细化'],['files','交付物']];let results='';
 if(state.tab==='cards'){const cards=v.artifacts.filter(a=>['process','system','question','ontology','binding','gap'].includes(a.kind)&&a.status!=='superseded');results=cards.map(card).join('')||(v.artifacts.some(a=>a.kind==='recommend')?empty('本轮尚无通过审核的业务卡片','请补充具体业务角色、操作步骤或数据口径后重新生成。'):empty('还没有业务卡片','运行业务推荐或业务理解，生成可逐条确认的结果。'));}
 else if(state.tab==='files'){results=`<p class="muted">交付包包含业务 YAML、OWL 本体、BPMN 流程与细化映射。下载时重新验证访问权限。</p>${v.editable?btn('生成交付包','export','','primary'):''}<div class="section-label">已有导出</div>${v.exports.map(e=>`<p><a href="/api/exports/${e.id}/download">↓ ${esc(e.name)}</a> <span class="muted">${date(e.created_at)}</span></p>`).join('')}`;}
 else {const arts=v.artifacts.filter(a=>a.kind===state.tab&&a.status!=='superseded');const a=arts.at(-1);results=a?card(a):empty('把信息变成可以讨论的结果','在左侧选择相应能力，结果会保留来源与版本。');if(state.tab==='refine'&&v.editable)results+=`<div class="actions">${btn('查看与编辑字段绑定','refinement-editor')}</div>`;}
 const active=v.jobs.filter(j=>['queued','running','retry_wait'].includes(j.status));const last=v.jobs.at(-1);
 shell(`<div class="page-head"><div><button class="link-button" data-action="nav" data-page="workspaces">← 所有工作区</button><h1>${esc(w.name)}</h1><p>${esc(workflow.objective||'每一条原话与资料，都是下一步判断的依据。')}</p></div><div class="actions">${v.editable?(state.lastRevisions?.[w.id]?btn('撤销上次修订','undo-revision'):'')+btn('重命名','rename')+btn('共享','share')+btn('归档','delete-workspace'):badge('只读')}</div></div><div class="flow">${workflow.stages.map((stage,i)=>`<div class="step ${stage.completed||stage.active?'active':''}"><b>${i+1}</b>${esc(stage.name)}</div>`).join('')}</div><div class="workbench"><section class="pane"><div class="pane-head"><strong>业务对话与资料</strong><span class="muted">${v.evidence.length} 条证据</span></div><div class="conversation"><div class="message"><div class="author">FDE ASSISTANT</div>先告诉我这家公司做什么、服务谁，以及最想解决的问题。你也可以上传制度和专业资料，再结合数据结构一起分析。</div>${v.evidence.filter(e=>e.active).map(e=>`<div class="message"><div class="author">业务原话 · ${date(e.created_at)}</div>${esc(e.text)}</div>`).join('')}</div>${v.editable?`<form class="composer" data-kind="evidence"><textarea name="text" id="composer" placeholder="例如：我们做食品批发，库存经常断货，希望优化补货…" required>${esc(state.draft)}</textarea><div class="actions"><button type="submit" class="primary">保存业务资料</button>${btn('上传目录 / DDL','catalog')}${btn('专业资料','workspace-knowledge')}</div></form><div class="toolbar">${['summarize','interview','recommend','match','report','revise','refine'].map(c=>btn(labels[c],'run-agent',`data-capability="${c}" ${active.length?'disabled':''}`)).join('')}</div>`:''}${active.map(j=>`<div class="job"><span class="loading-dot"></span>${esc(labels[j.capability]||j.capability)} ${badge(j.status)}<small>可离开页面，后台继续处理</small></div>`).join('')}${last&&last.status==='failed'?`<div class="job">${badge('failed')}<span>${esc(errors[last.error]||last.error)}</span>${v.editable?btn('重试任务','retry-job',`data-id="${last.id}"`):''}</div>`:''}<div class="pane-head"><span class="muted">已接入 ${v.catalogs.length} 份数据目录</span>${workspaceModelSelector()}</div></section><section class="pane"><div class="pane-head"><strong>业务产出物</strong><span class="muted">工作区版本 ${w.version}</span></div><div class="tabs">${tabs.map(([id,label])=>`<button data-action="tab" data-tab="${id}" class="${state.tab===id?'active':''}">${label}</button>`).join('')}</div><div class="results">${results}</div></section></div>`);
 if(active.length){clearTimeout(state.poll);state.poll=setTimeout(async()=>{try{if(state.current?.workspace.id===w.id&&state.page==='workspaces'){state.current=await api('/workspaces/'+w.id);renderWorkspace();}}catch(e){toast(e.message);}},2000);}
}
function card(a){const structured=['process','system','question','ontology','binding','gap'].includes(a.kind);const edit=state.current.editable&&!a.source_unavailable;let controls='';
 if(structured&&edit) controls=`<div class="actions">${btn('接受','decide',`data-id="${a.id}" data-decision="accept"`)}${btn('修改','edit-card',`data-id="${a.id}"`)}${btn('驳回','decide',`data-id="${a.id}" data-decision="reject"`)}</div>`;
 if(a.kind==='revise'&&a.status==='proposed'&&a.body.changes?.length&&edit)controls=`<div class="actions">${btn('选择并应用修改','apply-revision',`data-id="${a.id}"`,'primary')}</div>`;
 if(a.kind==='summarize'&&edit&&a.body.challenge_questions?.length){controls+=`<div class="nested"><div class="detail-title">下一步：回答挑战与目标</div>${a.body.challenge_questions.map((q,i)=>`<form data-kind="challenge-answer" data-index="${i}"><div class="detail-value"><strong>${esc(q.text)}</strong></div>${q.options?.length?`<p class="muted">参考：${q.options.map(esc).join(' / ')}</p>`:''}<textarea name="answer" rows="2" placeholder="回答后继续访谈" required></textarea><div class="actions"><button type="submit">保存回答并继续</button></div></form>`).join('')}</div>`;}if(a.kind==='interview'&&edit&&a.body.questions?.length){controls+=a.body.questions.map(q=>`<form data-kind="interview-answer" data-question="${esc(q.id||q.artifact_id)}"><div class="section-label">${esc(q.text)}</div>${q.options?.length?`<p class="muted">参考选项：${q.options.map(esc).join(' / ')}</p>`:''}<textarea name="answer" rows="2" placeholder="回答这个问题" required></textarea><div class="actions"><button type="submit">记录回答并继续追问</button>${btn('跳过','skip-question',`data-question="${esc(q.id||q.artifact_id)}"`)}</div></form>`).join('');}
 const refs=(a.citations||[]).map(c=>`<button class="citation" data-action="citation" data-id="${c.chunk_id}">${esc(c.title)} · 第 ${c.page} 页</button>`).join('');
 return `<article class="result-card"><div class="actions no-margin"><span class="muted">${labels[a.kind]||a.kind} · v${a.version}</span><span class="spacer"></span>${badge(a.status)}</div>${structured?`<h3>${esc(a.body.statement||a.body.object?.name||'业务判断')}</h3>`:''}<div class="result-content">${details(structured?a.body.object||a.body:a.body)}</div>${refs}${controls}</article>`;
}
async function knowledgePage(){state.kbs=await api('/knowledge-bases'+(state.knowledgeWorkspace?'?workspace_id='+state.knowledgeWorkspace:''));if(!state.kbs.find(k=>k.id===state.selectedKb))state.selectedKb=state.kbs[0]?.id;let kb=state.selectedKb?await api('/knowledge-bases/'+state.selectedKb+(state.knowledgeWorkspace?'?workspace_id='+state.knowledgeWorkspace:'')):null;
 shell(`<div class="page-head"><div><div class="eyebrow">GOVERNED KNOWLEDGE</div><h1>让专业资料，参与每次理解。</h1><p>版本、来源与授权一起保存，引用可以回到具体段落。</p></div><div class="actions">${btn('检索预览','search')}${btn('＋ 新建知识库','new-kb','','primary')}</div></div><div class="split"><div class="kb-list">${state.kbs.map(k=>`<div class="card ${k.id===state.selectedKb?'selected':''}" data-action="select-kb" data-id="${k.id}"><span class="pill gray">${k.scope==='workspace'?'工作区':'组织'}</span><h3>${esc(k.name)}</h3><span class="muted">${esc(k.description||'专业资料集合')}</span></div>`).join('')||empty('还没有知识库','创建项目或组织资料库。')}</div><div>${kb?`<div class="card"><div class="page-head"><div><h2>${esc(kb.knowledge_base.name)}</h2><p class="muted">${esc(kb.knowledge_base.description)}</p></div><div class="actions">${admin()&&kb.knowledge_base.scope==='tenant'?btn('授权','grant'):''}${btn('上传资料','upload-document','','primary')}</div></div><div class="info-box">支持 TXT、Markdown、PDF、DOCX。上传后异步检查与索引；受限资料不会发送给模型或向量服务。</div><div class="table-wrap"><table><thead><tr><th>文档与来源</th><th>版本</th><th>状态</th><th>操作</th></tr></thead><tbody>${kb.documents.map(d=>`<tr><td><strong>${esc(d.name)}</strong><div class="muted">${esc(d.source)}</div></td><td>v${d.current_version}</td><td>${badge(d.versions[0]?.status||'queued')}${d.versions[0]?.error?`<p class="muted">${esc(d.versions[0].error)}</p>`:''}</td><td><div class="actions"><a href="/api/documents/${d.id}/download${state.knowledgeWorkspace?'?workspace_id='+state.knowledgeWorkspace:''}">下载</a>${btn('新版本','upload-document',`data-id="${d.id}"`)}${btn('撤销','revoke-document',`data-id="${d.id}"`,'danger')}</div></td></tr>`).join('')}</tbody></table></div>${kb.grants.length?`<div class="section-label">当前授权</div>${kb.grants.map(g=>`<p class="small">${esc(g.subject_type)} · ${esc(roleNames[g.subject_id]||g.subject_id)} ${btn('撤销授权','revoke-grant',`data-id="${g.id}"`)}</p>`).join('')}`:''}</div>`:empty('组织知识库与项目资料分开管理','创建知识库后上传第一份专业资料。')}</div></div>`);
}
async function membersPage(){const members=await api('/members');state.members=members;shell(`<div class="page-head"><div><div class="eyebrow">TEAM & ACCESS</div><h1>让合适的人，一起参与。</h1><p>组织角色与工作区共享分别控制，停用后即时撤销访问。</p></div>${btn('邀请成员','invite','','primary')}</div><div class="table-wrap"><table><thead><tr><th>成员</th><th>邮箱</th><th>角色</th><th>状态</th><th>操作</th></tr></thead><tbody>${members.map(m=>`<tr><td>${esc(m.name)}</td><td>${esc(m.email)}</td><td>${roleNames[m.role]}</td><td>${badge(m.active?'有效':'已停用')}</td><td>${m.role!=='tenant_owner'&&m.user_id!==state.session.user.id?btn('管理','edit-member',`data-id="${m.user_id}"`):'—'}</td></tr>`).join('')}</tbody></table></div>`);}

function syncRefinementForm(form){
  const slot=form.querySelector('[name=slot_id]').value;
  const prior=state.refinement.bindings.find(binding=>binding.slot_id===slot)?.body||{};
  for(const name of ['action','fields','grain','scope','note']){
    const value=prior[name]??(name==='action'?'bind':'');
    form.querySelector(`[name=${name}]`).value=Array.isArray(value)?value.join(','):value;
  }
}
function providerModels(value){
  const values=Array.isArray(value)?value:String(value||'').split(',');
  return [...new Set(values.map(model=>String(model).trim()).filter(Boolean))];
}

function preferredProviderModel(models, preferred=''){
  if(models.includes(preferred))return preferred;
  return models.find(model=>! /^(gpt-image|dall-e|text-embedding|embedding|whisper|tts|rerank)(?:[-_.]|$)/i.test(model.split('/').at(-1)))||models[0]||'';
}
function providerCatalogMarkup(values, preferred=''){
  const models=providerModels(values), selected=preferredProviderModel(models,preferred);
  return `<section class="provider-catalog-section" aria-label="模型目录">
    <div class="provider-model-heading"><div><h3>模型目录</h3><span class="muted" id="provider-model-count">已添加 ${models.length} 个模型</span></div>${btn('获取可用模型','fetch-provider-models')}</div>
    <label for="f-default_model">默认模型</label><select id="f-default_model" name="default_model" ${models.length?'required':'disabled'}>${models.length?models.map(model=>`<option value="${esc(model)}" ${model===selected?'selected':''}>${esc(model)}</option>`).join(''):'<option value="">添加模型后选择</option>'}</select>
    <p class="muted catalog-help">工作区优先使用默认对话模型，也可在工作区单独切换。</p>
    ${models.length?`<input type="search" id="provider-catalog-search" aria-label="搜索已添加模型" placeholder="搜索已添加的模型…" autocomplete="off">`:''}
    <div class="provider-catalog-list" role="list" aria-label="已添加模型">${models.map(model=>`<div class="provider-model-row ${model===selected?'is-default':''}" role="listitem" data-model="${esc(model)}"><input type="hidden" name="models" value="${esc(model)}"><div class="provider-model-name"><span title="${esc(model)}">${esc(model)}</span>${model===selected?'<span class="pill">默认</span>':''}</div><div class="provider-model-actions">${model!==selected?btn('设为默认','provider-model-default',`data-model="${esc(model)}" aria-label="将 ${esc(model)} 设为默认"`):''}${btn('移除','provider-model-remove',`data-model="${esc(model)}" aria-label="移除 ${esc(model)}"`,'danger')}</div></div>`).join('')||'<div class="catalog-empty">还没有添加模型。获取模型目录，或在下方手动添加。</div>'}</div>
    <p id="provider-catalog-no-match" class="muted" hidden>没有匹配的模型</p>
    <div class="provider-manual-add"><input id="provider-manual-model" aria-label="手动添加模型 ID" placeholder="输入模型 ID，如 deepseek-chat" maxlength="128" autocomplete="off">${btn('＋ 添加模型','provider-model-add')}</div>
    <p id="provider-catalog-message" class="muted catalog-help" role="status" aria-live="polite">${models.length?'修改将在保存提供商后生效。':'至少添加一个模型后才能保存。'}</p>
  </section>`;
}
function readProviderForm(form){
  const data=new FormData(form);
  return {...Object.fromEntries(data.entries()),models:providerModels(data.getAll('models'))};
}
function filterProviderCatalog(form,query){
  const rows=[...form.querySelectorAll('.provider-model-row')];
  rows.forEach(row=>{row.hidden=!row.dataset.model.toLowerCase().includes(query.trim().toLowerCase());});
  const noMatch=form.querySelector('#provider-catalog-no-match');
  if(noMatch)noMatch.hidden=rows.length===0||rows.some(row=>!row.hidden);
}
function updateProviderCatalog(form,models,defaultModel,message=''){
  const query=form.querySelector('#provider-catalog-search')?.value||'';
  form.querySelector('#provider-catalog').innerHTML=providerCatalogMarkup(models,defaultModel);
  const search=form.querySelector('#provider-catalog-search');
  if(search){search.value=query;filterProviderCatalog(form,query);}
  const submitButton=form.querySelector('button[type=submit]');
  if(submitButton)submitButton.disabled=models.length===0;
  if(message)form.querySelector('#provider-catalog-message').textContent=message;
}
function editProviderModel(button){
  const form=button.closest('form'), values=readProviderForm(form), model=button.dataset.model;
  if(button.dataset.action==='provider-model-default'){
    updateProviderCatalog(form,values.models,model,'默认模型已切换，保存后生效。');return;
  }
  if(button.dataset.action==='provider-model-remove'){
    const remaining=values.models.filter(value=>value!==model);
    const selected=preferredProviderModel(remaining,values.default_model);
    updateProviderCatalog(form,remaining,selected,model===values.default_model&&selected?`已移除模型，默认模型改为 ${selected}。`:'模型已移除，保存后生效。');return;
  }
  const input=form.querySelector('#provider-manual-model'), id=input.value.trim();
  const message=form.querySelector('#provider-catalog-message');
  if(!id||id.length>128||/[\s,，]/.test(id)){
    message.textContent='请输入一个有效的模型 ID（最多 128 个字符，不含空格或逗号）。';input.focus();return;
  }
  if(values.models.includes(id)){message.textContent='这个模型已在目录中，无需重复添加。';input.focus();return;}
  updateProviderCatalog(form,[...values.models,id],values.default_model,'模型已添加，保存后生效。');
  const search=form.querySelector('#provider-catalog-search');
  if(search){search.value=id;filterProviderCatalog(form,id);}
  form.querySelector('#provider-manual-model').focus();
}
function resetProviderModelChoices(providerId){
  for(const [scope,key] of Object.entries(state.workspaceModels||{})){
    if(JSON.parse(key)[0]===providerId)delete state.workspaceModels[scope];
  }
}
function providerModal(kind, provider={}){
  const edit=kind==='edit-provider', models=providerModels(provider.models), protocol=provider.protocol||'openai';
  const body=`<p class="muted">配置模型连接，选择工作区使用的默认模型。</p><details class="provider-connection" ${edit?'':'open'}><summary>连接设置<span class="muted">${esc(provider.api_base||'API 地址、协议与密钥')}</span></summary><div class="provider-connection-fields">`+
    (edit?'':`<div>${field('Provider ID','provider_id',provider.provider_id||'sudorouter','text','required minlength="2" maxlength="80" pattern="[a-z][a-z0-9_\\-]+" title="以小写字母开头，仅包含小写字母、数字、下划线或连字符"')}</div>`)+
    `<div ${edit?'class="provider-field-wide"':''}>${field('显示名称','display_name',provider.display_name||'SudoRouter','text','required maxlength="120"')}</div>`+
    `<div>${field('API 地址','api_base',provider.api_base||'https://napi.sudorouter.ai/v1','url','required')}</div>`+
    `<div><label for="provider-protocol">协议</label><select id="provider-protocol" name="protocol"><option value="openai" ${protocol==='openai'?'selected':''}>OpenAI Chat Completions</option><option value="anthropic" ${protocol==='anthropic'?'selected':''}>Anthropic Messages</option></select></div>`+
    `<div class="provider-field-wide">${field(edit?'API 密钥（留空保持原值）':'API 密钥','api_key',provider.api_key||'','password',edit?'autocomplete="off"':'required autocomplete="off"')}</div></div></details>`+
    `<div id="provider-catalog">${providerCatalogMarkup(models,provider.default_model)}</div>`;
  modal(edit?'编辑模型提供商':'添加模型提供商',body,kind,edit?`data-id="${esc(provider.provider_id)}"`:'',edit?'保存提供商':'创建提供商');
  const submitButton=dialog.querySelector('button[type=submit]');
  if(submitButton)submitButton.disabled=models.length===0;
}

function restoreProviderDraft(selected){
  const draft=state.providerDraft;
  if(!draft)throw new Error('配置表单已失效，请重新打开模型提供商');
  const models=providerModels(draft.values.models);
  const merged=selected?providerModels([...models,...selected]):models;
  const defaultModel=preferredProviderModel(merged,draft.values.default_model);
  providerModal(draft.kind,{...draft.values,models:merged,default_model:defaultModel,provider_id:draft.id||draft.values.provider_id});
  state.providerDraft=null;
}

async function fetchProviderModels(button){
  const form=button.closest('form');
  const values=readProviderForm(form);
  const providerId=form.dataset.id;
  let path='/models/providers/catalog',body={api_base:values.api_base,protocol:values.protocol,api_key:values.api_key};
  if(form.dataset.kind==='edit-provider'&&!values.api_key){
    path='/models/providers/'+encodeURIComponent(providerId)+'/catalog';
    body={api_base:values.api_base,protocol:values.protocol};
  }else if(!values.api_key){
    throw new Error('请先填写 API 密钥');
  }
  button.disabled=true;
  button.textContent='正在获取…';
  const errorSlot=form.querySelector('.error-slot');
  errorSlot.textContent='';
  try{
    const result=await api(path,{method:'POST',body});
    if(!form.isConnected||!dialog.open)return;
    state.providerDraft={kind:form.dataset.kind,id:providerId,values};
    providerModelPicker(result.models);
  }catch(error){
    errorSlot.innerHTML=`<div class="error-box">${esc(error.message)}</div>`;
  }finally{
    button.disabled=false;
    button.textContent='获取可用模型';
  }
}

function providerModelPicker(models){
  const options=models.map(model=>`<label class="model-option"><input type="checkbox" name="selected_models" value="${esc(model)}" checked><span>${esc(model)}</span></label>`).join('');
  const body=`<p class="muted">已获取 ${models.length} 个可用模型，勾选后添加到模型目录。</p><div class="model-picker-toolbar"><input id="model-search" type="search" placeholder="搜索模型"><div class="actions">${btn('全选/取消全选','model-toggle-all')}${btn('取消全选','model-clear')}</div></div><div class="model-picker-list">${options||empty('没有可用模型','模型服务没有返回可用模型。')}</div>`;
  modal('选择要添加的模型',body,'model-picker','', '添加所选');
}

async function providersPage(){
  const providers=await api('/models/providers'); state.providers=providers;
  shell(`<div class="page-head"><div><div class="eyebrow">PERSONAL MODEL ACCESS</div><h1>我的模型</h1><p>为当前账号配置可用的 SudoRouter 或兼容模型服务。密钥加密保存，只在模型调用时使用。</p></div>${btn('＋ 添加模型提供商','new-provider','','primary')}</div><div class="grid">${providers.map(p=>{
    const models=providerModels(p.models),selected=preferredProviderModel(models,p.default_model);
    const preview=[selected,...models.filter(model=>model!==selected)].filter(Boolean).slice(0,5);
    return `<article class="card provider-card"><div class="actions"><span class="ws-icon">◇</span><span class="spacer"></span>${badge(p.api_key_configured?'密钥已配置':'未配置密钥')}</div><h3>${esc(p.display_name)}</h3><p class="muted">${esc(p.api_base)} · ${esc(p.protocol)}</p><div class="provider-card-default"><span class="muted">默认模型</span><strong>${esc(selected||'尚未选择')}</strong></div><div class="provider-model-heading"><span class="muted">模型目录 · ${models.length} 个模型</span>${p.verified_at?badge('已验证'):''}</div><div class="provider-model-chips">${preview.map(model=>`<span class="model-chip ${model===selected?'is-default':''}" title="${esc(model)}">${esc(model)}</span>`).join('')}${models.length>preview.length?`<span class="muted">另有 ${models.length-preview.length} 个</span>`:''}</div><div class="actions provider-card-actions">${btn('管理模型与默认项','edit-provider',`data-id="${esc(p.provider_id)}"`)}${btn('验证连接','test-provider',`data-id="${esc(p.provider_id)}"`)}${btn('删除','delete-provider',`data-id="${esc(p.provider_id)}"`,'danger')}</div></article>`;
  }).join('')||empty('还没有模型提供商','添加一个 SudoRouter API 或兼容 OpenAI 的模型服务。')}</div>`);
}

function adminShell(content){
  app.innerHTML=`<div class="layout"><aside class="sidebar"><div class="brand"><i>S</i>Sudo <span class="muted">Control</span></div><div class="eyebrow">平台管理</div><nav class="nav">${[['admin','▦','平台概览'],['admin-tenants','◫','租户管理'],['admin-users','♧','用户管理'],['admin-jobs','◌','任务与模型'],['admin-audit','≋','全局审计']].map(([id,icon,label])=>`<button data-action="admin-nav" data-page="${id}" class="${state.page===id?'active':''}"><span>${icon}</span>${label}</button>`).join('')}</nav><div class="sidebar-footer"><div class="user">${esc(state.session.user.name)}</div><p class="muted">平台管理员</p><div class="actions">${btn('返回业务端','business-home')}${btn('退出','logout')}</div></div></aside><div class="shell"><header class="topbar"><div class="breadcrumb">平台管理 <span>/</span> ${esc(state.adminTitle||'平台概览')}</div><div class="top-right"><span class="pill">全局权限</span><span class="muted">${esc(state.session.user.email)}</span></div></header><main class="content">${content}</main></div></div>`;
}
async function adminOverview(){const [o,tenants]=await Promise.all([api('/admin/overview'),api('/admin/tenants')]);state.adminTenants=tenants;state.adminTitle='平台概览';adminShell(`<div class="page-head"><div><div class="eyebrow">CONTROL PLANE</div><h1>平台运营中心</h1><p>管理租户、用户、任务、模型连接与全局审计。</p></div></div><div class="stats">${[['租户',o.tenants],['用户',o.users],['工作区',o.workspaces],['活动任务',o.active_jobs],['失败任务',o.failed_jobs],['模型调用',o.model_calls??o.router_calls]].map(([k,v])=>`<div class="stat"><div class="label">${k}</div><div class="value">${v}</div></div>`).join('')}</div><div class="section-label"><span>最近租户</span>${btn('管理全部租户','admin-nav',`data-page="admin-tenants"`)}</div><div class="table-wrap"><table><thead><tr><th>组织</th><th>成员</th><th>工作区</th><th>状态</th><th>操作</th></tr></thead><tbody>${tenants.slice(0,10).map(t=>`<tr><td>${esc(t.name)}<p class="muted mono">${esc(t.id)}</p></td><td>${t.members}</td><td>${t.workspaces}</td><td>${badge(t.active?'有效':'已停用')}</td><td>${btn('查看','admin-tenant',`data-id="${t.id}"`)} ${btn('编辑','admin-edit-tenant',`data-id="${t.id}"`)}</td></tr>`).join('')}</tbody></table></div>`);}
async function adminTenants(){const tenants=await api('/admin/tenants');state.adminTenants=tenants;state.adminTitle='租户管理';adminShell(`<div class="page-head"><div><div class="eyebrow">TENANTS</div><h1>租户管理</h1><p>创建、编辑、停用和恢复租户，并查看成员、工作区和任务状态。</p></div><div class="actions">${btn('＋ 新建租户','admin-new-tenant','','primary')}</div></div><div class="table-wrap"><table><thead><tr><th>组织</th><th>成员</th><th>工作区</th><th>任务</th><th>状态</th><th>操作</th></tr></thead><tbody>${tenants.map(t=>`<tr><td><strong>${esc(t.name)}</strong><p class="muted mono">${esc(t.id)}</p></td><td>${t.members}</td><td>${t.workspaces}</td><td>${t.active_jobs}</td><td>${badge(t.active?'有效':'已停用')}</td><td>${btn('查看','admin-tenant',`data-id="${t.id}"`)} ${btn('编辑','admin-edit-tenant',`data-id="${t.id}"`)} ${btn(t.active?'停用':'恢复',t.active?'admin-suspend':'admin-restore',`data-id="${t.id}"`,'danger')} ${t.active?btn('删除','admin-delete-tenant',`data-id="${t.id}"`,'danger'):''}</td></tr>`).join('')}</tbody></table></div>`);}
async function adminTenant(id){const d=await api('/admin/tenants/'+id);state.adminTenant=d;state.adminTitle=d.tenant.name;adminShell(`<div class="page-head"><div><button class="link-button" data-action="admin-nav" data-page="admin-tenants">← 租户列表</button><h1>${esc(d.tenant.name)}</h1><p class="muted mono">${esc(d.tenant.id)}</p></div><div class="actions">${btn('编辑租户','admin-edit-tenant',`data-id="${d.tenant.id}"`)} ${d.tenant.active?btn('删除租户','admin-delete-tenant',`data-id="${d.tenant.id}"`,'danger'):''}</div></div><div class="stats"><div class="stat"><div class="label">成员</div><div class="value">${d.members.length}</div></div><div class="stat"><div class="label">工作区</div><div class="value">${d.workspaces.length}</div></div><div class="stat"><div class="label">额度上限</div><div class="value">${d.tenant.budget_micros}</div></div><div class="stat"><div class="label">Router 账户</div><div class="value">${d.tenant.router_account?'已绑定':'未绑定'}</div></div></div><div class="info-box">允许模型：${esc((d.tenant.allowed_models||[]).join('、')||'未限制')}</div><div class="section-label">成员</div><div class="table-wrap"><table><thead><tr><th>用户</th><th>邮箱</th><th>角色</th><th>状态</th></tr></thead><tbody>${d.members.map(m=>`<tr><td>${esc(m.user.name)}</td><td>${esc(m.user.email)}</td><td>${roleNames[m.role]||m.role}</td><td>${badge(m.active?'有效':'已停用')}</td></tr>`).join('')}</tbody></table></div>`);}
async function adminUsers(){const [users,tenants]=await Promise.all([api('/admin/users'),api('/admin/tenants')]);state.adminUsers=users;state.adminTenants=tenants;state.adminTitle='用户管理';adminShell(`<div class="page-head"><div><div class="eyebrow">USERS & MEMBERSHIPS</div><h1>全局用户</h1><p>停用用户、授予平台权限，并把用户关联到一个或多个租户。</p></div></div><div class="table-wrap"><table><thead><tr><th>用户</th><th>租户关联</th><th>平台管理员</th><th>状态</th><th>操作</th></tr></thead><tbody>${users.map(u=>`<tr><td><strong>${esc(u.name)}</strong><p class="muted">${esc(u.email)}</p></td><td>${u.memberships.length?u.memberships.map(m=>`<div class="small">${esc(m.tenant_name||m.tenant_id)} · ${roleNames[m.role]||m.role} ${badge(m.active?'有效':'已停用')} ${btn('调整','admin-edit-membership',`data-user-id="${u.id}" data-tenant-id="${m.tenant_id}"`)}</div>`).join(''):'<span class="muted">未关联租户</span>'}</td><td>${u.platform_admin?'是':'否'}</td><td>${badge(u.active?'有效':'已停用')}</td><td>${btn('关联租户','admin-assign-user',`data-id="${u.id}"`)} ${btn(u.active?'停用':'恢复','admin-user-status',`data-id="${u.id}" data-active="${u.active?'false':'true'}"`)} ${!u.platform_admin?btn('授予平台管理员','admin-promote',`data-id="${u.id}"`):''}</td></tr>`).join('')}</tbody></table></div>`);}
async function adminJobs(){const jobs=await api('/admin/jobs');state.adminTitle='任务与模型';adminShell(`<div class="page-head"><div><div class="eyebrow">JOBS & AI</div><h1>任务与模型运行</h1><p>按组织查看后台任务和模型调用状态。</p></div></div><div class="table-wrap"><table><thead><tr><th>时间</th><th>租户</th><th>能力</th><th>模型</th><th>状态</th><th>错误</th></tr></thead><tbody>${jobs.map(j=>`<tr><td>${date(j.created_at)}</td><td class="mono code-id">${esc(j.tenant_id)}</td><td>${esc(labels[j.capability]||j.capability)}</td><td>${esc(j.model||'—')}</td><td>${badge(j.status)}</td><td>${esc(j.error||'—')}</td></tr>`).join('')}</tbody></table></div>`);}
async function organizationPage(){
  const tenant=await api('/tenant');
  shell(`<div class="page-head"><div><div class="eyebrow">ORGANIZATION</div><h1>组织设置</h1><p>维护当前组织的信息，与团队有序协作。</p></div>${btn('管理成员','nav','data-page="members"')}</div><section class="card organization-settings"><h2>基本信息</h2><form data-kind="organization-settings">${field('组织名称','name',tenant.name,'text','required maxlength="150"')}<p class="muted">组织名称不可重复。名称调整会记录到审计日志。</p><div class="error-slot"></div><div class="actions"><button class="primary" type="submit">保存设置</button></div></form></section>`);
}
function auditQuery(){return `?offset=${state.auditOffset||0}&exclude_reads=${!state.auditShowReads}`;}
function auditControls(rows){return `<div class="section-label"><label class="row"><input type="checkbox" id="audit-show-reads" ${state.auditShowReads?'checked':''}>包含工作区访问记录</label><div class="actions">${btn('上一页','audit-prev',(state.auditOffset||0)?'':'disabled')}<span class="muted">第 ${Math.floor((state.auditOffset||0)/200)+1} 页</span>${btn('下一页','audit-next',rows.length===200?'':'disabled')}</div></div>`;}
async function reloadAudit(){if(state.page==='admin-audit')await adminAudit();else await auditPage();}
function auditTable(rows,global=false){
  const headings=['时间',...(global?['租户']:[]),'操作','操作人','相关资源','详情'];
  return `<div class="table-wrap"><table class="audit-table"><thead><tr>${headings.map(title=>`<th>${title}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>{
    const display=row.display||{},actor=display.actor||{},resource=display.resource||{};
    const raw=[['事件编号',row.id],['操作代码',row.action],['租户编号',row.tenant_id],['操作人编号',row.user_id],['资源编号',row.resource_id],['工作区编号',row.workspace_id]].filter(([,value])=>value);
    return `<tr><td class="audit-time">${esc(new Date(row.created_at).toLocaleString('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}))}</td>${global?`<td>${esc(display.tenant||'历史租户（名称不可用）')}</td>`:''}<td><span class="audit-action">${esc(display.action||'其他操作（详见技术详情）')}</span></td><td><strong>${esc(actor.name||'系统自动执行')}</strong>${actor.email?`<div class="muted">${esc(actor.email)}</div>`:''}</td><td><span class="audit-resource-type">${esc(resource.type||'资源')}</span><div>${esc(resource.name||'历史资源（名称不可用）')}</div>${resource.workspace?`<div class="muted">所属工作区：${esc(resource.workspace)}</div>`:''}</td><td>${(display.details||[]).map(item=>`<div class="audit-detail"><span>${esc(item.label)}：</span>${esc(item.value)}</div>`).join('')}<details class="audit-technical"><summary>技术详情</summary><dl>${raw.map(([label,value])=>`<dt>${label}</dt><dd>${esc(value)}</dd>`).join('')}</dl>${Object.keys(row.details||{}).length?`<div class="muted">原始事件参数</div><pre>${esc(JSON.stringify(row.details,null,2))}</pre>`:''}</details></td></tr>`;
  }).join('')||`<tr><td colspan="${headings.length}" class="audit-empty">暂无符合条件的审计记录</td></tr>`}</tbody></table></div>`;
}
async function adminAudit(){const rows=await api('/admin/audit'+auditQuery());state.adminTitle='全局审计';adminShell(`<div class="page-head"><div><div class="eyebrow">AUDIT</div><h1>全局审计</h1><p>查看各租户由谁、在何时执行了什么操作，以及涉及的业务资源。</p></div></div>${auditControls(rows)}${auditTable(rows,true)}`);}
async function billingPage(){const data=await api('/billing');state.billing=data;const calls=data.calls;const personal=data.personal_calls||[];const amounts={};calls.forEach(c=>amounts[c.currency]=(amounts[c.currency]||0)+c.amount_micros);const amount=Object.keys(amounts).length>1?'多币种':((Object.values(amounts)[0]||0)/1e6).toFixed(4);shell(`<div class="page-head"><div><div class="eyebrow">MODEL USAGE & ATTRIBUTION</div><h1>每次模型调用，都有来处。</h1><p>余额与结算以 SudoRouter 为准，这里展示调用归因和回执。</p></div><div class="actions">${state.session.role==='tenant_owner'?btn(data.account?'更换绑定':'绑定 Router','bind-router','','primary')+btn('模型策略','policy'):''}${admin()?btn('执行对账','reconcile'):''}</div></div><div class="stats">${[['可用额度',data.account?((data.account.available_micros||0)/1e6).toFixed(4):'未绑定'],['最近调用',calls.length],['展示记录金额',amount],['待处理',calls.filter(c=>c.status!=='settled').length]].map(([label,value])=>`<div class="stat"><div class="label">${label}</div><div class="value">${value}</div><div class="muted">${esc(data.account?.currency||'SudoRouter')}</div></div>`).join('')}</div><div class="table-wrap"><table><thead><tr><th>时间 / 能力</th><th>模型</th><th>Token</th><th>金额</th><th>结算 / 对账</th></tr></thead><tbody>${calls.map(c=>`<tr><td>${date(c.created_at)}<p class="muted">${esc(labels[c.capability]||c.capability)}</p></td><td>${esc(c.model)}<p class="mono code-id" title="${esc(c.router_request_id)}">${esc(c.router_request_id||'等待回执')}</p></td><td>${c.tokens_in} / ${c.tokens_out}</td><td>${(c.amount_micros/1e6).toFixed(6)} ${esc(c.currency)}</td><td>${badge(c.status)}<p class="muted">${c.reconciled?'已核对':'待核对'}</p></td></tr>`).join('')}</tbody></table></div><div class="section-label">个人提供商用量</div><p class="muted">记录已完成任务的模型调用次数与 Token。费用由个人提供商结算，平台不估算金额。</p><div class="table-wrap"><table><thead><tr><th>时间 / 能力</th><th>提供商 / 模型</th><th>调用次数</th><th>输入 / 输出 Token</th></tr></thead><tbody>${personal.map(c=>`<tr><td>${date(c.created_at)}<p>${esc(labels[c.capability]||c.capability)}</p></td><td>${esc(c.provider_id)}<p>${esc(c.model)}</p></td><td>${c.calls}</td><td>${c.tokens_in} / ${c.tokens_out}</td></tr>`).join('')||'<tr><td colspan="4">暂无已完成的个人模型任务</td></tr>'}</tbody></table></div>`);}
async function auditPage(){const [rows,metrics]=await Promise.all([api('/audit'+auditQuery()),api('/metrics')]);shell(`<div class="page-head"><div><div class="eyebrow">AUDIT & OPERATIONS</div><h1>协作有记录，变化可追溯。</h1><p>记录权限、资料、生成、导出与账务事件，审计正文不可修改。</p></div></div><div class="stats">${[['任务',metrics.counts.jobs],['失败任务',metrics.failed_jobs],['知识片段',metrics.counts.knowledge_chunks],['模型 Token',metrics.tokens_in+metrics.tokens_out]].map(([k,v])=>`<div class="stat"><div class="label">${k}</div><div class="value">${v}</div></div>`).join('')}</div>${auditControls(rows)}${auditTable(rows)}`);}
async function navigate(page){clearTimeout(state.poll);state.page=page;state.current=null;if(page==='workspaces')await workspaceList();else if(page==='knowledge')await knowledgePage();else if(page==='providers')await providersPage();else if(page==='members')await membersPage();else if(page==='organization')await organizationPage();else if(page==='billing')await billingPage();else if(page==='admin')await adminOverview();else await auditPage();}
async function navigateAdmin(page){state.page=page; if(page==='admin')return adminOverview();if(page==='admin-tenants')return adminTenants();if(page==='admin-users')return adminUsers();if(page==='admin-jobs')return adminJobs();if(page==='admin-audit')return adminAudit();}
async function action(el){const a=el.dataset.action,id=el.dataset.id;if(a==='close'){if(el.closest('form')?.dataset.kind==='model-picker'){restoreProviderDraft();return;}state.providerDraft=null;dialog.close();return;}if(a==='auth-mode'){state.authMode=el.dataset.mode;renderAuth();return;}if(a==='logout'){await api('/auth/logout',{method:'POST'});state.session=null;renderAuth();return;}if(a==='model-clear'){document.querySelectorAll('input[name="selected_models"]').forEach(input=>{input.checked=false;});return;}if(a==='model-toggle-all'){const inputs=[...document.querySelectorAll('input[name="selected_models"]')];const selectAll=inputs.some(input=>!input.checked);inputs.forEach(input=>{input.checked=selectAll;});return;}if(a==='audit-prev'||a==='audit-next'){state.auditOffset=Math.max(0,(state.auditOffset||0)+(a==='audit-next'?200:-200));await reloadAudit();return;}if(a==='admin-nav'){if(el.dataset.page==='admin-tenants'||el.dataset.page==='admin-users'||el.dataset.page==='admin-jobs'||el.dataset.page==='admin-audit'||el.dataset.page==='admin')history.pushState({},'', '/admin');await navigateAdmin(el.dataset.page);return;}if(a==='business-home'){history.pushState({},'', '/');await navigate('workspaces');return;}if(a==='admin-tenant'){await adminTenant(id);return;}if(a==='admin-new-tenant'){modal('新建租户',field('租户名称','name','','text','required')+field('额度上限（微单位）','budget_micros','200000','number','min="100" required')+field('模型白名单（逗号分隔，可留空）','allowed_models'), 'admin-new-tenant');return;}if(a==='admin-edit-tenant'){const t=(state.adminTenants||[]).find(x=>x.id===id)||state.adminTenant?.tenant;if(!t)return;modal('编辑租户',field('租户名称','name',t.name)+field('额度上限（微单位）','budget_micros',t.budget_micros,'number','min="100" required')+field('模型白名单（逗号分隔，可留空）','allowed_models',(t.allowed_models||[]).join(','))+`<label>状态</label><select name="active"><option value="true" ${t.active?'selected':''}>有效</option><option value="false" ${!t.active?'selected':''}>停用</option></select>`, 'admin-edit-tenant',`data-id="${id}"`);return;}if(a==='admin-delete-tenant'){modal('删除租户', '<p>删除操作会停用租户并撤销普通成员的访问资格，数据和审计记录会保留。</p>', 'admin-delete-tenant',`data-id="${id}"`);return;}if(a==='admin-assign-user'){const u=(state.adminUsers||[]).find(x=>x.id===id);const tenants=(state.adminTenants||[]).filter(t=>t.active);if(!u||!tenants.length){toast('没有可关联的有效租户');return;}modal('关联用户到租户',`<p>${esc(u.name)} · ${esc(u.email)}</p><label>租户</label><select name="tenant_id">${tenants.map(t=>`<option value="${t.id}">${esc(t.name)}</option>`).join('')}</select><label>角色</label><select name="role"><option value="fde_member">FDE 成员</option><option value="tenant_admin">组织管理员</option><option value="tenant_owner">组织所有者</option><option value="billing_viewer">账务查看者</option></select>`, 'admin-assign-user',`data-id="${id}"`);return;}if(a==='admin-edit-membership'){const uid=el.dataset.userId,tid=el.dataset.tenantId;const u=(state.adminUsers||[]).find(x=>x.id===uid);const m=u?.memberships.find(x=>x.tenant_id===tid);if(!u||!m)return;modal('调整租户关联',`<p>${esc(u.name)} · ${esc(u.email)}</p><label>租户</label><input value="${esc(m.tenant_name||m.tenant_id)}" disabled><label>角色</label><select name="role">${['tenant_owner','tenant_admin','fde_member','billing_viewer'].map(r=>`<option value="${r}" ${m.role===r?'selected':''}>${roleNames[r]}</option>`).join('')}</select><label>状态</label><select name="active"><option value="true" ${m.active?'selected':''}>有效</option><option value="false" ${!m.active?'selected':''}>停用</option></select>`, 'admin-edit-membership',`data-user-id="${uid}" data-tenant-id="${tid}"`);return;}if(a==='admin-remove-membership'){await api(`/admin/users/${el.dataset.userId}/memberships/${el.dataset.tenantId}`,{method:'DELETE'});await adminUsers();return;}if(a==='admin-suspend'){await api('/admin/tenants/'+id+'/suspend',{method:'POST'});await adminTenants();return;}if(a==='admin-restore'){await api('/admin/tenants/'+id+'/restore',{method:'POST'});await adminTenants();return;}if(a==='admin-user-status'){await api('/admin/users/'+id+'/status',{method:'PATCH',body:{role:'fde_member',active:el.dataset.active==='true'}});await adminUsers();return;}if(a==='admin-promote'){await api('/admin/users/'+id+'/platform-admin',{method:'POST'});await adminUsers();return;}if(a==='nav'){if(el.dataset.page!=='knowledge')state.knowledgeWorkspace=null;await navigate(el.dataset.page);return;}if(a==='new-workspace'){modal('新建私有工作区',field('工作区名称','name','','text','required')+area('这次希望解决什么问题？','objective'),'new-workspace');return;}if(a==='new-tenant'){modal('创建组织',field('组织名称','name','','text','required'),'new-tenant');return;}if(a==='open-workspace'){state.tab='auto';await openWorkspace(id);return;}if(a==='toggle-deleted'){state.deleted=!state.deleted;await workspaceList();return;}if(a==='restore'){await api('/workspaces/'+id+'/restore',{method:'POST'});await workspaceList();toast('工作区已取消归档');return;}if(a==='tab'){state.tab=el.dataset.tab;renderWorkspace();return;}
 if(a==='rename'){modal('重命名工作区',field('名称','name',state.current.workspace.name),'rename');return;}if(a==='delete-workspace'){modal('归档工作区',`<p>归档后，工作区及其历史资料会保留，可在「已归档」中取消归档，继续使用。</p>`,'delete-workspace','','归档');return;}if(a==='share'){const [members,candidates]=await Promise.all([api('/workspaces/'+state.current.workspace.id+'/members'),api('/workspaces/'+state.current.workspace.id+'/collaborators')]);modal('共享工作区',`<label>组织成员</label><select name="user_id">${candidates.filter(m=>m.id!==state.session.user.id).map(m=>`<option value="${m.id}">${esc(m.name)} · ${esc(m.email)}</option>`).join('')}</select>`+`<label>角色</label><select name="role"><option value="workspace_viewer">查看者</option><option value="workspace_editor">编辑者</option></select><div class="section-label">现有成员</div>${members.map(m=>`<p class="small">${esc(candidates.find(c=>c.id===m.user_id)?.name||'组织成员')} · ${roleNames[m.role]} ${btn('移除','remove-ws-member',`data-id="${m.user_id}"`)}</p>`).join('')}`,'share');return;}if(a==='remove-ws-member'){await api(`/workspaces/${state.current.workspace.id}/members/${id}`,{method:'DELETE'});dialog.close();toast('已撤销工作区访问');return;}
 if(a==='run-agent'){const capability=el.dataset.capability;if(capability==='revise'){modal('提出修订意见',area('希望修改什么','instruction'),'revise');return;}await queueAgent(capability);return;}if(a==='catalog'){modal('接入数据目录',`<p class="muted">在客户内网用 fde-scan 扫描后上传 catalog JSON，或直接上传 SQL DDL。平台不会在线连接客户数据库。</p><input class="file-input" name="file" type="file" accept=".json,.sql" required>`,'catalog');return;}
 if(a==='workspace-knowledge'){state.knowledgeWorkspace=state.current.workspace.id;await navigate('knowledge');return;}if(a==='decide'){const art=state.current.artifacts.find(x=>x.id===id);await api('/artifacts/'+id+'/decisions',{method:'POST',body:{action:el.dataset.decision,version:art.version,reason:el.dataset.decision==='reject'?'人工驳回':''}});await openWorkspace(state.current.workspace.id);return;}if(a==='edit-card'){const art=state.current.artifacts.find(x=>x.id===id);modal('修改业务判断',area('业务定义','statement',art.body.statement||'')+field('名称','name',art.body.object?.name||''),'edit-card',`data-id="${id}"`);return;}
 if(a==='skip-question'){await queueAgent('interview',{answers:[{question_id:el.dataset.question,evidence_ids:[],skipped:true}]});return;}if(a==='retry-job'){await api('/jobs/'+id+'/retry',{method:'POST'});await openWorkspace(state.current.workspace.id);return;}if(a==='export'){const e=await api('/workspaces/'+state.current.workspace.id+'/exports',{method:'POST'});toast('交付包已生成，可点击下载');await openWorkspace(state.current.workspace.id);return;}if(a==='citation'){const c=await api('/chunks/'+id+'?workspace_id='+state.current.workspace.id);modal(c.title,`<p class="muted">第 ${c.page} 页 · 第 ${c.paragraph} 段 · v${c.version}</p><div class="detail-value">${esc(c.text)}</div>`,'close-only');return;}
 if(a==='undo-revision'){const wid=state.current.workspace.id;await api('/revisions/'+state.lastRevisions[wid]+'/undo',{method:'POST'});delete state.lastRevisions[wid];await openWorkspace(state.current.workspace.id);toast('已撤销上次修订');return;}if(a==='apply-revision'){const art=state.current.artifacts.find(x=>x.id===id);modal('选择需要应用的修改',art.body.changes.map(c=>`<label class="row"><input type="checkbox" name="changes" value="${esc(c.artifact_id)}" checked><span>${esc(c.before||'')} → ${esc(c.after||'删除')}</span></label>`).join(''),'apply-revision',`data-id="${id}"`);return;}
 if(a==='refinement-editor'){const m=await api('/workspaces/'+state.current.workspace.id+'/refinement');state.refinement=m;modal('字段绑定与业务细化',`<label for="refine-slot">需要细化的条目</label><select name="slot_id" id="refine-slot">${m.slots.map(s=>`<option value="${s.id}">${esc(s.parent+' · '+s.label)}</option>`).join('')}</select><label for="refine-action">结论</label><select id="refine-action" name="action"><option value="bind">绑定字段</option><option value="gap">数据缺口</option><option value="offline">线下步骤</option></select>${field('字段（表.字段，多个用逗号分隔）','fields')}${field('粒度字段（可留空）','grain')}${field('范围与口径','scope')}${area('说明','note')}`,'refine-binding');syncRefinementForm(dialog.querySelector('form'));return;}
 if(a==='select-kb'){state.selectedKb=id;await knowledgePage();return;}if(a==='new-kb'){const ws=await api('/workspaces');modal('创建知识库',field('名称','name','','text','required')+area('说明','description')+`<label>范围</label><select name="workspace_id">${admin()?'<option value="">组织知识库</option>':''}${ws.map(w=>`<option value="${w.id}" ${w.id===state.knowledgeWorkspace?'selected':''}>工作区 · ${esc(w.name)}</option>`).join('')}</select>`,'new-kb');return;}if(a==='upload-document'){modal(id?'上传文档新版本':'上传专业资料',`<input class="file-input" type="file" name="file" accept=".txt,.md,.pdf,.docx" required>${field('来源说明','source','业务资料上传')}${field('标签（逗号分隔）','tags')}<label>资料分类</label><select name="classification"><option value="internal">组织内部</option><option value="public">公开资料</option></select><input type="hidden" name="document_id" value="${id||''}">`,'upload-document');return;}if(a==='revoke-document'){modal('撤销文档',`<p>撤销后立即退出检索，后续任务不再使用该资料。</p>`,'revoke-document',`data-id="${id}"`);return;}if(a==='grant'){const [members,workspaces]=await Promise.all([api('/members'),api('/workspaces')]);state.grantChoices={role:[{id:'fde_member',name:'所有 FDE 成员'}],user:members.filter(m=>m.active&&m.role!=='billing_viewer').map(m=>({id:m.user_id,name:m.name+' · '+m.email})),workspace:workspaces};modal('授予知识库访问',`<label>授权对象</label><select name="subject_type" id="grant-type"><option value="role">组织角色</option><option value="user">用户</option><option value="workspace">工作区</option></select><label>选择范围</label><select name="subject_id" id="grant-subject"><option value="fde_member">所有 FDE 成员</option></select>`,'grant');return;}if(a==='revoke-grant'){await api(`/knowledge-bases/${state.selectedKb}/grants/${id}`,{method:'DELETE'});await knowledgePage();return;}
 if(a==='search'){const ws=await api('/workspaces');modal('预览授权检索',`<label>以哪个工作区的权限检索</label><select name="workspace_id">${ws.map(w=>`<option value="${w.id}">${esc(w.name)}</option>`).join('')}</select>${field('检索问题','query','','text','required')}`,'search');return;}
 if(a==='invite'){modal('邀请组织成员',field('邮箱','email','','email','required')+`<label>角色</label><select name="role"><option value="fde_member">FDE 成员</option>${state.session.role==='tenant_owner'?'<option value="tenant_admin">组织管理员</option>':''}<option value="billing_viewer">账务查看者</option></select>`,'invite');return;}if(a==='edit-member'){const m=state.members.find(x=>x.user_id===id);modal('管理成员',`<p>${esc(m.name)} · ${esc(m.email)}</p><label>角色</label><select name="role">${['fde_member','tenant_admin','billing_viewer'].map(r=>`<option value="${r}" ${m.role===r?'selected':''}>${roleNames[r]}</option>`).join('')}</select><label>状态</label><select name="active"><option value="true" ${m.active?'selected':''}>有效</option><option value="false" ${!m.active?'selected':''}>停用</option></select>`,'edit-member',`data-id="${id}"`);return;}
 if(['provider-model-add','provider-model-remove','provider-model-default'].includes(a)){editProviderModel(el);return;}if(a==='new-provider'){providerModal('new-provider');return;}if(a==='edit-provider'){const p=state.providers.find(x=>x.provider_id===id);providerModal('edit-provider',p);return;}if(a==='fetch-provider-models'){await fetchProviderModels(el);return;}if(a==='test-provider'){const result=await api('/models/providers/'+id+'/test',{method:'POST'});toast(`连接成功，已发现 ${result.models.length} 个模型`);await providersPage();return;}if(a==='delete-provider'){modal('删除模型提供商','删除后该账号不能再使用此提供商，历史任务不受影响。','delete-provider',`data-id="${id}"`);return;}if(a==='bind-router'){modal('绑定 SudoRouter 账户',`<p class="muted">填写由 SudoRouter 签发的一次性组织绑定码。余额和充值由 Router 管理。</p>${field('绑定授权码','authorization_code','','text','required')}`,'bind-router');return;}if(a==='policy'){modal('模型与预算策略',field('允许模型（逗号分隔）','models',state.billing.policy.models.join(','))+field('单任务预算上限（账户币种）','budget_micros',state.billing.policy.budget_micros/1e6,'number','step="0.0001" min="0.0001"'),'policy');return;}if(a==='reconcile'){const r=await api('/billing/reconcile',{method:'POST'});toast(`核对 ${r.checked} 次调用，一致 ${r.matched}，差异 ${r.mismatched}`);await billingPage();}
}
async function queueAgent(capability,params={}){
  const choice=selectedWorkspaceModel();
  if(!choice)throw new Error('请先在「我的模型」配置对话模型，或绑定组织模型账户');
  await api(`/workspaces/${state.current.workspace.id}/agent/${capability}`,{method:'POST',body:{call_id:crypto.randomUUID().replaceAll('-',''),model:choice.model,provider_id:choice.provider_id,params}});
  state.tab=capability==='recommend'?'cards':capability;
  await openWorkspace(state.current.workspace.id);
  toast('任务已提交，后台开始处理');
}
async function submit(form){const kind=form.dataset.kind;const fd=new FormData(form),d=Object.fromEntries(fd.entries()),id=form.dataset.id;const w=state.current?.workspace.id;
 if(kind==='close-only'){dialog.close();return;}if(kind==='auth'){const mode=form.dataset.mode;const path={login:'/auth/login',invite:'/invitations/accept',reset:'/auth/password-reset/confirm',forgot:'/auth/password-reset'}[mode];if(!path)throw new Error('该账号入口已关闭，请返回登录');const r=await api(path,{method:'POST',body:d});if(['forgot','reset'].includes(mode)){toast(r.message||'密码已更新，请登录');state.authMode='login';renderAuth();return;}const adminEntry=location.pathname==='/admin';await refreshSession();history.replaceState({},'',adminEntry&&state.session.platform_admin?'/admin':'/');if(adminEntry&&state.session.platform_admin)await navigateAdmin('admin');else await navigate(state.session.role==='billing_viewer'?'billing':'workspaces');return;}
 if(kind==='new-workspace'){const ws=await api('/workspaces',{method:'POST',body:d});dialog.close();state.tab='summarize';await openWorkspace(ws.id);return;}
 if(kind==='organization-settings'){await api('/tenant',{method:'PATCH',body:{name:d.name}});await refreshSession();await organizationPage();toast('组织信息已保存');return;}
 if(kind==='new-tenant'){const t=await api('/tenants',{method:'POST',body:d});await api('/tenants/'+t.id+'/switch',{method:'POST'});await refreshSession();dialog.close();await navigate('workspaces');return;}
 if(kind==='admin-new-tenant'){const body={name:d.name,budget_micros:Number(d.budget_micros),allowed_models:d.allowed_models.split(',').map(x=>x.trim()).filter(Boolean)};await api('/admin/tenants',{method:'POST',body});dialog.close();await adminTenants();return;}
 if(kind==='admin-edit-tenant'){const body={name:d.name,budget_micros:Number(d.budget_micros),allowed_models:d.allowed_models.split(',').map(x=>x.trim()).filter(Boolean),active:d.active==='true'};await api('/admin/tenants/'+id,{method:'PATCH',body});dialog.close();await adminTenants();return;}
 if(kind==='admin-delete-tenant'){await api('/admin/tenants/'+id,{method:'DELETE'});dialog.close();await adminTenants();return;}
 if(kind==='admin-assign-user'){await api('/admin/users/'+id+'/memberships',{method:'POST',body:{tenant_id:d.tenant_id,role:d.role}});dialog.close();await adminUsers();return;}
 if(kind==='admin-edit-membership'){await api('/admin/users/'+form.dataset.userId+'/memberships/'+form.dataset.tenantId,{method:'PATCH',body:{role:d.role,active:d.active==='true'}});dialog.close();await adminUsers();return;}
 if(kind==='model-picker'){const models=providerModels(fd.getAll('selected_models'));if(!models.length)throw new Error('至少选择一个模型');restoreProviderDraft(models);return;}
 if(kind==='rename')await api('/workspaces/'+w,{method:'PATCH',body:d});
 if(kind==='delete-workspace'){await api('/workspaces/'+w,{method:'DELETE'});dialog.close();await workspaceList();toast('工作区已归档');return;}
 if(kind==='share')await api('/workspaces/'+w+'/members',{method:'POST',body:d});
 if(kind==='evidence'){
   const choice=selectedWorkspaceModel();
   const r=await api('/workspaces/'+w+'/evidence',{method:'POST',body:{text:d.text,start_ai:Boolean(choice),...(choice?{model:choice.model,provider_id:choice.provider_id}:{})}});
   state.draft='';
   if(r.next_job)state.tab='summarize';
   await openWorkspace(w);
   toast(r.ai_error?'资料已保存，AI 未启动：'+r.ai_error.message:r.next_job?'资料已保存，AI 正在生成业务概要':'业务资料已保存');
   return;
 }
 if(kind==='interview-answer'){const ev=await api('/workspaces/'+w+'/evidence',{method:'POST',body:{text:d.answer}});await queueAgent('interview',{answers:[{question_id:form.dataset.question,evidence_ids:[ev.id]}]});return;}if(kind==='challenge-answer'){await api('/workspaces/'+w+'/evidence',{method:'POST',body:{text:d.answer}});await queueAgent('interview');return;}if(kind==='catalog')await api('/workspaces/'+w+'/catalogs',{method:'POST',body:fd});
 if(kind==='revise'){const ev=await api('/workspaces/'+w+'/evidence',{method:'POST',body:{text:d.instruction}});dialog.close();await queueAgent('revise',{instruction_evidence_id:ev.id});return;}
 if(kind==='edit-card'){const art=state.current.artifacts.find(x=>x.id===id);const body=structuredClone(art.body);body.statement=d.statement;if(body.object&&'statement' in body.object)body.object.statement=d.statement;if(body.object&&d.name)body.object.name=d.name;await api('/artifacts/'+id+'/decisions',{method:'POST',body:{action:'edit',version:art.version,body}});}
 if(kind==='apply-revision'){const r=await api('/artifacts/'+id+'/revision/apply',{method:'POST',body:{change_ids:fd.getAll('changes')}});state.lastRevisions ||= {};state.lastRevisions[w]=r.id;toast('修订已应用，可在工作区顶部撤销');}
 if(kind==='refine-binding'){const prior=state.refinement.bindings.find(b=>b.slot_id===d.slot_id);await api('/workspaces/'+w+'/refinement',{method:'POST',body:{...d,fields:d.fields.split(',').map(x=>x.trim()).filter(Boolean),grain:d.grain.split(',').map(x=>x.trim()).filter(Boolean),version:prior?.version||0}});}
 if(['rename','share','catalog','edit-card','apply-revision','refine-binding'].includes(kind)){dialog.close();await openWorkspace(w);return;}
 if(kind==='new-kb'){const kb=await api('/knowledge-bases',{method:'POST',body:{...d,workspace_id:d.workspace_id||null}});state.selectedKb=kb.id;}
 if(kind==='upload-document'){if(!d.document_id)fd.delete('document_id');const r=await api(`/knowledge-bases/${state.selectedKb}/documents`,{method:'POST',body:fd});toast('资料已上传，正在后台检查与索引');pollIngestion(r.job.id);}
 if(kind==='revoke-document')await api('/documents/'+id,{method:'DELETE'});
 if(kind==='grant')await api(`/knowledge-bases/${state.selectedKb}/grants`,{method:'POST',body:d});
 if(['new-kb','upload-document','revoke-document','grant'].includes(kind)){dialog.close();await knowledgePage();return;}
 if(kind==='search'){const r=await api('/knowledge/search',{method:'POST',body:d});dialog.close();modal('授权检索结果',r.hits.length?r.hits.map(h=>`<div class="result-card"><h3>${esc(h.title)} · 第 ${h.page} 页</h3><div class="detail-value">${esc(h.text)}</div></div>`).join(''):empty('没有匹配的授权资料','检查资料索引状态与授权范围。'),'close-only');return;}
 if(kind==='invite'){const r=await api('/invitations',{method:'POST',body:d});dialog.close();if(r.development_link)modal('邀请已创建',`<p>邀请链接已生成，可交给测试成员：</p><textarea readonly rows="4">${esc(r.development_link)}</textarea>`,'close-only');else toast('邀请邮件已发送');return;}
 if(kind==='edit-member'){await api('/members/'+id,{method:'PATCH',body:{role:d.role,active:d.active==='true'}});dialog.close();await membersPage();return;}
 if(kind==='new-provider'||kind==='edit-provider'){
   const body=readProviderForm(form);
   if(!body.models.length)throw new Error('请至少添加一个模型');
   body.default_model=preferredProviderModel(body.models,body.default_model);
   const edit=kind==='edit-provider';
   if(edit&&!body.api_key)delete body.api_key;
   const result=await api('/models/providers'+(edit?'/'+encodeURIComponent(id):''),{method:edit?'PATCH':'POST',body});
   if(edit&&state.providers?.find(provider=>provider.provider_id===id)?.default_model!==result.default_model)resetProviderModelChoices(id);
   dialog.close();await refreshSession();await providersPage();return;
 }if(kind==='delete-provider'){await api('/models/providers/'+id,{method:'DELETE'});dialog.close();await refreshSession();await providersPage();return;}if(kind==='bind-router'){await api('/billing/binding',{method:'POST',body:d});await refreshSession();}
 if(kind==='policy')await api('/billing/policy',{method:'PATCH',body:{models:d.models.split(',').map(x=>x.trim()).filter(Boolean),budget_micros:Math.round(Number(d.budget_micros)*1e6)}});
 if(['bind-router','policy'].includes(kind)){dialog.close();await billingPage();}
}
function pollIngestion(jobId){setTimeout(async()=>{try{const j=await api('/jobs/'+jobId);if(['queued','running','retry_wait'].includes(j.status))pollIngestion(jobId);else{toast(j.status==='succeeded'?'专业资料已完成索引':'资料处理失败：'+j.error);if(state.page==='knowledge')await knowledgePage();}}catch(e){toast(e.message);}},1800);}
document.addEventListener('click',async e=>{
  const accountMenu=document.querySelector('.account-menu[open]');
  if(accountMenu&&!accountMenu.contains(e.target))accountMenu.open=false;
  const el=e.target.closest('[data-action]');
  if(!el)return;
  try{await action(el);}catch(err){toast(err.message);}
});
document.addEventListener('keydown',e=>{
  if(e.key!=='Escape')return;
  const accountMenu=document.querySelector('.account-menu[open]');
  if(accountMenu){accountMenu.open=false;accountMenu.querySelector('summary').focus();e.preventDefault();}
});
document.addEventListener('submit',async e=>{const form=e.target.closest('form[data-kind]');if(!form)return;e.preventDefault();const button=form.querySelector('button[type=submit]');if(button)button.disabled=true;try{await submit(form);}catch(err){const slot=form.querySelector('.error-slot');if(slot)slot.innerHTML=`<div class="error-box">${esc(err.message)}${err.code?`<div class="muted">${esc(err.code)}</div>`:''}</div>`;else toast(err.message);}finally{if(button)button.disabled=false;}});
document.addEventListener('input',e=>{if(e.target.id==='composer')state.draft=e.target.value;if(e.target.id==='provider-catalog-search')filterProviderCatalog(e.target.closest('form'),e.target.value);if(e.target.id==='model-search'){const query=e.target.value.trim().toLowerCase();document.querySelectorAll('.model-option').forEach(option=>{option.style.display=option.textContent.toLowerCase().includes(query)?'flex':'none';});}});
document.addEventListener('change',async e=>{if(e.target.id==='audit-show-reads'){state.auditShowReads=e.target.checked;state.auditOffset=0;try{await reloadAudit();}catch(err){toast(err.message);}return;}if(e.target.id==='refine-slot')syncRefinementForm(e.target.closest('form'));if(e.target.id==='f-default_model'&&e.target.closest('form')?.querySelector('#provider-catalog')){const form=e.target.closest('form'),values=readProviderForm(form);updateProviderCatalog(form,values.models,e.target.value,'默认模型已切换，保存后生效。');form.querySelector('#f-default_model').focus();}if(e.target.id==='model-select'){selectedWorkspaceModel();}if(e.target.id==='grant-type'){document.querySelector('#grant-subject').innerHTML=(state.grantChoices[e.target.value]||[]).map(x=>`<option value="${esc(x.id)}">${esc(x.name)}</option>`).join('');}if(e.target.id==='tenant-switch'){try{await api('/tenants/'+e.target.value+'/switch',{method:'POST'});await refreshSession();state.selectedKb=null;state.knowledgeWorkspace=null;await navigate(state.session.role==='billing_viewer'?'billing':'workspaces');}catch(err){toast(err.message);}}});
(async()=>{if(location.pathname==='/admin'){try{await refreshSession();if(!state.session.platform_admin)throw new Error('无平台管理权限');await navigateAdmin('admin');}catch{renderAuth();}return;}if(location.pathname==='/invite'){state.authMode='invite';renderAuth();return;}if(location.pathname==='/reset-password'){state.authMode='reset';renderAuth();return;}try{await refreshSession();await navigate(state.session.role==='billing_viewer'?'billing':'workspaces');}catch{renderAuth();}})();

document.addEventListener('keydown',e=>{if(['Enter',' '].includes(e.key)&&e.target.matches('[data-action][tabindex]')){e.preventDefault();e.target.click();}});

dialog.addEventListener('cancel',e=>{if(dialog.querySelector('form')?.dataset.kind==='model-picker'){e.preventDefault();restoreProviderDraft();}else{state.providerDraft=null;}});

document.addEventListener('keydown',e=>{if(e.target.id==='provider-manual-model'&&e.key==='Enter'){e.preventDefault();editProviderModel(e.target.closest('form').querySelector('[data-action=provider-model-add]'));}});
