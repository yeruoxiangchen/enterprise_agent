const $ = (q) => document.querySelector(q);
const escape = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let me, bases = [], conversationId = null, activeRun = null, feedbackRun = null, currentTab = 'chat', toastTimer;
let stream = null, pendingUpload = null, lastSubmission = null, chatVersion = 0;
const incidentLabels = {error_code:'错误码', symptom:'故障现象', environment:'运行环境', software:'软件'};
function resetChatInput() {
  chatVersion++;
  stream?.close(); stream = null;
  pendingUpload?.abort(); pendingUpload = null;
  lastSubmission = null;
  $('#question').readOnly = false;
  $('#question').value = '';
  clearAttachments();
}
function renderIncidentReadings(element, diagnosis) {
  element.replaceChildren();
  for (const item of diagnosis?.observations || []) {
    const title = document.createElement('strong');
    title.textContent = '系统读到 · ' + item.filename;
    const readings = document.createElement('div');
    const values = item.observed.map(f => f.value + (f.certain ? '' : '（识别不清）'));
    readings.textContent = values.slice(0,8).join(' · ') || '未读到可用于判断的报错文字。';
    element.append(title, readings);
    if (values.length > 8) {
      const more = document.createElement('details'), summary = document.createElement('summary');
      summary.textContent = '查看其余 ' + (values.length - 8) + ' 项';
      more.append(summary, values.slice(8).join(' · '));
      element.append(more);
    }
    for (const [field, value] of Object.entries(item.answers)) {
      const correction = document.createElement('div');
      correction.textContent = '员工补充 · ' + incidentLabels[field] + '：' + value;
      element.append(correction);
    }
  }
  element.classList.toggle('hidden', !element.childElementCount);
}
const statuses = {queued:'排队中',processing:'正在处理',ready:'已就绪',error:'处理失败',answered:'已完成',partial_success:'部分完成',insufficient:'资料不足',clarify:'待补充信息',running:'执行中',cancelled:'已停止'};
function toast(message) { $('#toast').textContent = message; $('#toast').classList.remove('hidden'); clearTimeout(toastTimer); toastTimer = setTimeout(() => $('#toast').classList.add('hidden'), 4500); }
async function api(path, options = {}) {
  const headers = {'X-KB-Client':'web', ...(options.headers || {})};
  if (options.body && !(options.body instanceof FormData)) { headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(options.body); }
  const response = await fetch('/api' + path, {...options, headers, credentials:'same-origin'});
  if (!response.ok) { const error = await response.json().catch(() => ({})); if (response.status === 401 && path !== '/login') showLogin(); throw new Error(typeof error.detail === 'string' ? error.detail : '请求失败，请检查输入后重试'); }
  return response.json();
}
function showLogin() {
  resetChatInput(); activeRun = null; conversationId = null; setBusy(false);
  $('#chat-messages').innerHTML = welcomeMarkup;
  showEvidence({});
  $('#workspace').classList.add('hidden'); $('#login-view').classList.remove('hidden'); me = null;
}
function date(value) { return new Date(value * 1000).toLocaleString('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}); }
async function enter() {
  me = await api('/me'); $('#login-view').classList.add('hidden'); $('#workspace').classList.remove('hidden');
  $('#username').textContent = me.username; $('#role-name').textContent = me.role === 'admin' ? '空间管理员' : '团队成员'; $('#avatar').textContent = me.username.slice(0,1).toUpperCase();
  document.querySelectorAll('.admin-only').forEach(el => el.classList.toggle('hidden', me.role !== 'admin'));
  $('#mode-badge').textContent = me.workflow === 'business_multi' ? '多 Agent 企业服务' : me.workflow === 'fixed_rag' ? '知识库问答' : me.mode === 'agent' ? '模型问答' : '原文检索';
  $('#mode-hint').textContent = me.workflow === 'business_multi' ? '技术支持 · 制度流程 · 服务办理；工单需确认提交' : me.workflow === 'fixed_rag' ? '先检索相关资料，再生成带来源的回答；资料不足时说明不足' : me.mode === 'agent' ? '根据已读取的文档生成回答' : '当前提供原文摘录，未启用模型生成';
  $('#ticket-nav').classList.remove('hidden');
  await Promise.all([refreshBases(), refreshHistory()]);
  const saved = sessionStorage.getItem('kb-current-chat:'+me.id);
  if (saved && [...document.querySelectorAll('[data-conversation]')].some(b => b.dataset.conversation === saved)) {
    await openConversation(saved);
  }
}
async function refreshBases() {
  bases = await api('/bases');
  for (const selector of ['#chat-base','#document-base']) {
    const previous = $(selector).value;
    $(selector).innerHTML = (selector === '#chat-base' ? '<option value="">全部可访问知识库</option>' : '') + bases.map(b => `<option value="${escape(b.id)}">${escape(b.name)}</option>`).join('');
    if ([...$(selector).options].some(o => o.value === previous)) $(selector).value = previous;
  }
  $('#base-cards').innerHTML = bases.map(b => `<article class="base-card"><h3>${escape(b.name)}</h3><p>${b.document_count} 份文档 · ${b.visibility === 'all' ? '全部成员' : '仅管理员'}</p><p>${escape(b.description)}</p></article>`).join('') || '<p class="muted">还没有知识库，请管理员创建一个。</p>';
}
async function refreshHistory() { const history = await api('/conversations'); $('#history').innerHTML = history.map(c => `<button data-conversation="${escape(c.id)}" title="${escape(c.title)}">${escape(c.title)}</button>`).join('') || '<div class="small muted">开始提问后会保存在这里</div>'; }
async function tab(name) {
  currentTab = name; for (const t of ['chat','documents','feedback','tickets']) $(`#${t}-view`).classList.toggle('hidden', t !== name);
  document.querySelectorAll('[data-tab]').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  $('#page-name').textContent = {chat:'知识问答',documents:'文档资料',feedback:'用户反馈',tickets:'服务工单'}[name];
  if (name === 'documents') { await refreshBases(); await refreshDocuments(); }
  if (name === 'feedback') await refreshFeedback();
  if (name === 'tickets') await refreshTickets();
}
const welcomeMarkup = $('#chat-messages').innerHTML;
function newChat() { sessionStorage.removeItem('kb-current-chat:'+me.id); resetChatInput(); conversationId = null; activeRun = null; $('#chat-messages').innerHTML = welcomeMarkup; setBusy(false); showEvidence({citations:[], events:[]}); tab('chat').catch(e=>toast(e.message)); $('#question').focus(); }
function syncSendButton() {
  const busy = $('#question-form').getAttribute('aria-busy') === 'true';
  $('#send').disabled = busy || (!$('#question').value.trim() && !draftAttachments.length);
}
function setBusy(value) {
  $('#question-form').setAttribute('aria-busy', String(value));
  $('#incident-open').disabled = value;
  $('#attachments').querySelectorAll('button').forEach(button => { button.disabled = value; });
  $('#cancel-run').classList.toggle('hidden', !value || !activeRun);
  syncSendButton();
}
function message(question, run) {
  const block = document.createElement('div'); block.dataset.runBlock = run.id;
  block.innerHTML = '<div class="message user"></div><div class="message answer"><div class="message-label"></div><div class="answer-body"></div><div class="answer-actions"></div></div>';
  const bubble = block.querySelector('.message.user');
  if (question) {
    const caption = document.createElement('span');
    caption.textContent = question;
    bubble.append(caption);
  }
  appendSentAttachments(bubble, run.incident_inputs);
  const readout = document.createElement('div'); readout.className = 'incident-readout';
  block.querySelector('.answer-body').before(readout);
  fillMessage(block, run); $('#chat-messages').append(block); return block;
}
function fillMessage(block, run) {
  const body = block.querySelector('.answer-body'), actions = block.querySelector('.answer-actions');
  renderIncidentReadings(block.querySelector('.incident-readout'), run.diagnosis);
  const label = run.status === 'clarify' && !run.diagnosis?.clarification ? '已回复' : statuses[run.status] || run.status;
  block.querySelector('.message-label').innerHTML = `✦ 知页 <span class="muted">· ${escape(label)}</span>`;
  body.classList.toggle('working', ['queued','running'].includes(run.status));
  body.textContent = run.answer || run.error || (run.status === 'cancelled' ? '已停止本次任务。' : run.status === 'queued' ? '消息已发送，正在等待处理…' : '正在处理这条消息…');
  actions.innerHTML = `<button data-inspect="${escape(run.id)}">来源与过程 ↗</button>` + (run.answer ? `<button data-feedback="${escape(run.id)}">评价回答</button>` : '');
  if (run.can_resume) actions.innerHTML += `<button data-resume="${escape(run.id)}">继续未完成步骤</button>`;
}
function showEvidence(run) {
  $('#sources').innerHTML = (run.citations || []).map(s => `<button class="source-card" data-document="${escape(s.document_id)}" data-chunk="${s.id}"><strong>[${escape(s.label)}] ${escape(s.filename)}</strong><small>${escape(s.base_name)}${s.page ? ' · 第 '+s.page+' 页' : ''}</small><p>${escape(s.text.slice(0,120))}…</p></button>`).join('') || '<div class="empty-small">尚未引用资料</div>';
  const names = {search:'检索资料',read_chunk:'读取原文',list_documents:'查看文档',finish:'提交答案'};
  const internalEvents = new Set(['business_context','model_request','model_response','native_graph_returned','incident_request','attachment_request','incident_diagnosis']);
  $('#trace').innerHTML = (run.events || []).filter(e => !internalEvents.has(e.kind)).map(e => {
    let title = e.kind, detail = '';
    const domains = {technical:'技术支持',policy:'制度流程',service:'服务办理'};
    if (e.kind === 'business_route') { title='业务分工'; detail=e.payload.tasks.map(t=>domains[t.domain]+'：'+t.question).join('；') || e.payload.clarification; }
    if (e.kind === 'business_worker') { title=domains[e.payload.domain]+' Agent'; detail=e.payload.question; }
    if (e.kind === 'business_search') { title='领域检索'; detail=domains[e.payload.domain]+' · '+e.payload.query+' · '+e.payload.sources.length+'条证据'; }
    if (e.kind === 'business_skill') { title='加载业务规范'; detail=domains[e.payload.domain]; }
    if (e.kind === 'business_experience') { title='参考处理经验'; detail=domains[e.payload.domain]+' · '+e.payload.ids.length+'条相关经验'; }
    if (e.kind === 'business_completed') { title='领域处理完成'; detail=domains[e.payload.domain]+' · '+(statuses[e.payload.status]||e.payload.status); }
    if (e.kind === 'business_model') { title='模型处理'; detail='累计 '+e.payload.calls+' 次请求'; }
    if (e.kind === 'ticket_draft') { title='工单草稿待确认'; detail=e.payload.title; }
    if (e.kind === 'queued') title = '任务已保存，等待处理';
    if (e.kind === 'incident_worker_started') title = '开始读取故障材料';
    if (e.kind === 'started') { title = e.payload.resumed ? '恢复任务' : '开始任务'; detail = e.payload.mode === 'agent' ? '模型问答' : '原文检索'; }
    if (e.kind === 'tool') { title = names[e.payload.name] || e.payload.name; const r = e.payload.result; detail = r.error || (r.results ? `找到 ${r.results.length} 条候选片段` : r.filename || '已完成'); if (r.warnings?.length) detail += '；' + r.warnings.join('；'); }
    if (e.kind === 'model') { title = '模型处理'; detail = `第 ${e.payload.turn} 轮`; }
    if (e.kind === 'native_application_started') { title = '开始处理'; detail = '知识查询与工单办理'; }
    if (e.kind === 'delegation_bound') { title = '专业任务分工'; detail = ({evidence:'资料核验',service:'服务办理'})[e.payload.subagent_type] || '专业处理'; }
    if (e.kind === 'tool_effect') { title = ({search_documents:'检索资料',read_incident:'读取故障材料',prepare_ticket_draft:'生成工单草稿',list_my_tickets:'查询我的工单',get_my_ticket:'读取工单进度'})[e.payload.name] || '业务操作'; detail = ({observed:'已取得结果',committed:'已保存',refused:'操作未执行',unknown:'等待核对'})[e.payload.status] || ''; }
    if (e.kind === 'native_application_error') { title = '处理暂未完成'; detail = '请查看任务状态，符合恢复条件时可继续办理'; }
    if (e.kind === 'application_contract_retry') { title = '补全资料核验'; detail = '正在补齐本次请求需要的依据'; }
    if (e.kind === 'effect_reused') { title = '沿用已完成的操作'; detail = '保留已有回执，避免重复办理'; }
    if (e.kind === 'finished') { title = '完成'; detail = `引用 ${e.payload.citations} 条来源`; }
    if (e.kind === 'error') { title = '执行失败'; detail = e.payload.message; }
    if (e.kind === 'cancelled') title = '任务停止';
    if (e.kind === 'task_plan') { title = '拆解问题'; detail = e.payload.tasks.map(t=>t.question).join('；'); }
    if (e.kind === 'agent_search') { title = '查找子问题资料'; detail = e.payload.query; }
    if (e.kind === 'task_completed') { title = '子问题处理完成'; detail = e.payload.task_id + ' · ' + (statuses[e.payload.status] || e.payload.status); }
    if (e.kind === 'skill_loaded') { title = '加载处理规范'; detail = ({lookup:'资料查询',procedure:'操作指导',troubleshoot:'故障排查'})[e.payload.id] || e.payload.id; }
    return `<div class="trace-item"><b>${escape(title)}</b><small>${escape(detail)}</small></div>`;
  }).join('') || '<p class="muted small">暂无执行记录</p>';
}
function monitor(runId, selectedConversation, after=0, snapshot=null) {
  stream?.close();
  activeRun = runId; setBusy(true);
  const source = new EventSource('/api/runs/'+runId+'/events?after='+after);
  stream = source;
  const run = snapshot || {id:runId, status:'queued', events:[]};
  const block = document.querySelector('[data-run-block="'+runId+'"]');
  function selected() { return stream === source && conversationId === selectedConversation; }
  function render(data) { if (block) fillMessage(block,data); showEvidence(data); }
  function finish(data) {
    if (!selected()) return;
    source.close(); stream = null; activeRun = null; setBusy(false);
    render(data);
    refreshHistory().catch(e=>toast(e.message));
  }
  source.onopen = () => { if (selected()) render(run); };
  source.onmessage = event => {
    if (!selected()) return;
    const entry = JSON.parse(event.data);
    entry.id = Number(event.lastEventId);
    if (entry.id <= (run.events.at(-1)?.id || 0)) return;
    run.events.push(entry);
    if (entry.kind === 'incident_diagnosis') run.diagnosis = entry.payload;
    if (!['queued','incident_request'].includes(entry.kind)) run.status = 'running';
    render(run);
  };
  source.addEventListener('done', event => finish(JSON.parse(event.data)));
  source.onerror = async () => {
    if (!selected()) return;
    if (source.readyState === EventSource.CONNECTING) {
      if (block) block.querySelector('.answer-body').textContent = '正在重新连接任务进度…';
      return;
    }
    try {
      const latest = await api('/runs/'+runId);
      if (!selected()) return;
      if (['queued','running'].includes(latest.status)) monitor(runId,selectedConversation,latest.events.at(-1)?.id || 0,latest);
      else finish(latest);
    } catch (error) {
      if (selected()) { source.close(); stream=null; activeRun=null; setBusy(false); toast(error.message); }
    }
  };
}
async function openConversation(id) {
  resetChatInput();
  const version = chatVersion;
  activeRun = null; conversationId = id; setBusy(true);
  try {
    const data = await api('/conversations/' + id);
    if (version !== chatVersion) return;
    sessionStorage.setItem('kb-current-chat:'+me.id, id);
    await tab('chat');
    if (version !== chatVersion) return;
    $('#chat-messages').replaceChildren();
    data.runs.forEach(run => message(run.question, run));
    const last = data.runs.at(-1);
    if (last) {
      const detail = await api('/runs/'+last.id);
      if (version !== chatVersion) return;
      fillMessage(document.querySelector('[data-run-block="'+last.id+'"]'), detail);
      showEvidence(detail);
      if (['queued','running'].includes(detail.status)) monitor(last.id, id, detail.events.at(-1)?.id || 0, detail);
    }
    $('#chat-messages').scrollTop = $('#chat-messages').scrollHeight;
  } finally {
    if (version === chatVersion && !activeRun) setBusy(false);
  }
}
async function refreshDocuments() {
  if (!me) return;
  const baseId = $('#document-base').value; const docs = await api('/documents' + (baseId ? '?base_id=' + encodeURIComponent(baseId) : ''));
  $('#documents').innerHTML = docs.map(d => `<tr><td><button class="text-button" data-document="${escape(d.id)}">▤ ${escape(d.filename)}</button>${d.error ? `<div class="error small">${escape(d.error)}</div>` : ''}</td><td><span class="status ${escape(d.status)}">${escape(statuses[d.status])}</span></td><td>${d.chunk_count}</td><td>${date(d.created_at)}</td><td>${me.role === 'admin' ? `<button class="text-button" data-reindex="${escape(d.id)}">重新索引</button><button class="text-button" data-delete="${escape(d.id)}">删除</button>` : '—'}</td></tr>`).join('') || '<tr><td colspan="5" class="muted">暂无文档。上传资料后，即可开始问答。</td></tr>';
}
async function source(id, chunk) {
  const data = await api('/documents/' + id); $('#source-title').textContent = data.filename; $('#source-download').href = '/api/documents/' + encodeURIComponent(id) + '/download';
  $('#source-content').innerHTML = data.chunks.map(c => `<div class="source-chunk" id="chunk-${c.id}"><small>片段 ${c.ordinal+1}${c.page ? ' · 第 '+c.page+' 页' : ''}</small><pre>${escape(c.text)}</pre></div>`).join('') || '<p class="muted">文档还未完成解析。</p>';
  $('#source-dialog').showModal(); if (chunk) document.getElementById('chunk-'+chunk)?.scrollIntoView({block:'start'});
}
async function refreshFeedback() { const rows = await api('/feedback'); $('#feedback-list').innerHTML = rows.map(f => `<article class="feedback-card"><small>${escape(f.username)} · ${date(f.created_at)} · ${f.rating === 1 ? '有帮助' : '需要改进'}</small><h3>${escape(f.question)}</h3><pre>${escape(f.answer)}</pre><p>${escape(f.comment || '未填写补充说明')}</p></article>`).join('') || '<p class="muted">还没有用户反馈。</p>'; }
$('#login-form').addEventListener('submit',async e=>{e.preventDefault(); const button=e.target.querySelector('button');button.disabled=true; $('#login-error').textContent=''; try{await api('/login',{method:'POST',body:Object.fromEntries(new FormData(e.target))});e.target.reset();await enter();}catch(err){$('#login-error').textContent=err.message;}finally{button.disabled=false;}});
$('#logout').addEventListener('click',async()=>{try{await api('/logout',{method:'POST'});showLogin();}catch(e){toast(e.message);}});
$('#new-chat').addEventListener('click',newChat);
$('#incident-open').addEventListener('click', () => $('#incident-file').click());
$('#incident-file').addEventListener('change', event => {
  addAttachments([...event.target.files]);
  event.target.value = '';
});
$('#question').addEventListener('paste', event => {
  const files = [...event.clipboardData.files];
  if (!files.length) return;
  if (!event.clipboardData.getData('text/plain')) event.preventDefault();
  addAttachments(files);
});
$('#question').addEventListener('input', syncSendButton);
$('#question-form').addEventListener('submit', async event => {
  event.preventDefault();
  const question = $('#question').value.trim(), items = [...draftAttachments];
  if (event.currentTarget.getAttribute('aria-busy') === 'true' || (!question && !items.length)) return;
  const version = chatVersion, origin = conversationId, userId = me.id, base = $('#chat-base').value;
  const upload = new AbortController();
  pendingUpload = upload;
  $('#question').readOnly = true;
  setBusy(true);
  try {
    const links = await uploadAttachments(items, upload.signal);
    if (version !== chatVersion) return;
    const payload = {question, conversation_id:origin, base_ids:base ? [base] : [], incident_ids:links.map(item => item.incident_id)};
    const body = JSON.stringify(payload);
    if (lastSubmission?.body !== body) {
      const bytes = crypto.getRandomValues(new Uint8Array(16));
      lastSubmission = {body, id:Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('')};
    }
    payload.client_message_id = lastSubmission.id;
    const result = await api('/chat', {method:'POST', body:payload});
    if (me?.id === userId) refreshHistory().catch(error => toast(error.message));
    if (version !== chatVersion) return;
    lastSubmission = null;
    conversationId = result.conversation_id;
    sessionStorage.setItem('kb-current-chat:'+userId, conversationId);
    $('#welcome')?.remove();
    const queued = {id:result.run_id, status:'queued', events:[], incident_inputs:links};
    message(question, queued);
    $('#question').value = '';
    clearAttachments();
    $('#chat-messages').scrollTop = $('#chat-messages').scrollHeight;
    monitor(result.run_id, conversationId, 0, queued);
  } catch (error) {
    if (version === chatVersion) { setBusy(false); toast(error.message); }
  } finally {
    if (pendingUpload === upload) {
      pendingUpload = null;
      $('#question').readOnly = false;
      $('#question').focus();
    }
  }
});
$('#question').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();$('#question-form').requestSubmit();}});
$('#cancel-run').addEventListener('click',async()=>{if(activeRun){try{await api('/runs/'+activeRun+'/cancel',{method:'POST'});toast('已请求停止，正在结束当前步骤');}catch(e){toast(e.message);}}});
$('#document-base').addEventListener('change',()=>refreshDocuments().catch(e=>toast(e.message)));
$('#upload').addEventListener('click',()=>{if(!$('#document-base').value){toast('请先创建知识库');return;}$('#file-input').click();});
$('#file-input').addEventListener('change',async e=>{const files=[...e.target.files];$('#upload').disabled=true;try{for(let i=0;i<files.length;i++){const form=new FormData();form.set('base_id',$('#document-base').value);form.set('file',files[i]);$('#upload-progress').textContent=`正在上传 ${i+1}/${files.length}：${files[i].name}`;const result=await api('/documents',{method:'POST',body:form});if(result.duplicate)toast(files[i].name+' 已存在，未重复导入');}$('#upload-progress').textContent='上传完成，后台正在解析文档。';await refreshBases();await refreshDocuments();}catch(err){$('#upload-progress').textContent=err.message;toast(err.message);}finally{$('#upload').disabled=false;e.target.value='';}});
$('#new-base').addEventListener('click',()=>$('#base-dialog').showModal());
$('#base-form').addEventListener('submit',async e=>{e.preventDefault();try{const result=await api('/bases',{method:'POST',body:Object.fromEntries(new FormData(e.target))});$('#base-dialog').close();e.target.reset();await refreshBases();$('#document-base').value=result.id;await refreshDocuments();toast('知识库已创建');}catch(err){toast(err.message);}});
$('#add-user').addEventListener('click',()=>$('#user-dialog').showModal());
$('#user-form').addEventListener('submit',async e=>{e.preventDefault();try{await api('/users',{method:'POST',body:Object.fromEntries(new FormData(e.target))});$('#user-dialog').close();e.target.reset();toast('成员账号已创建');}catch(err){toast(err.message);}});
$('#feedback-form').addEventListener('submit',async e=>{e.preventDefault();const body=Object.fromEntries(new FormData(e.target));body.rating=Number(body.rating);try{await api('/runs/'+feedbackRun+'/feedback',{method:'POST',body});$('#feedback-dialog').close();e.target.reset();toast('已收到反馈，谢谢');}catch(err){toast(err.message);}});
document.addEventListener('click',async e=>{const b=e.target.closest('button');if(!b)return;try{if(b.dataset.tab)await tab(b.dataset.tab);if(b.dataset.close)$('#'+b.dataset.close).close();if(b.dataset.conversation)await openConversation(b.dataset.conversation);if(b.closest('.suggestions') && !$('#question').readOnly){$('#question').value=b.textContent;syncSendButton();$('#question').focus();}if(b.dataset.document)await source(b.dataset.document,b.dataset.chunk);if(b.dataset.inspect){const run=await api('/runs/'+b.dataset.inspect);showEvidence(run);if(matchMedia('(max-width:800px)').matches){$('#source-title').textContent='来源与执行记录';$('#source-download').removeAttribute('href');$('#source-content').innerHTML=$('#sources').innerHTML+$('#trace').innerHTML;$('#source-dialog').showModal();}}if(b.dataset.feedback){feedbackRun=b.dataset.feedback;$('#feedback-dialog').showModal();}if(b.dataset.reindex){await api('/documents/'+b.dataset.reindex+'/reindex',{method:'POST'});await refreshDocuments();}if(b.dataset.delete&&confirm('删除该文档及其索引？历史回答保留，但原文将不可访问。')){await api('/documents/'+b.dataset.delete,{method:'DELETE'});await refreshBases();await refreshDocuments();}}catch(err){toast(err.message);}});
setInterval(()=>{if(me&&currentTab==='documents')refreshDocuments().catch(()=>{});},2500);
document.addEventListener('click', async e => {
  const button = e.target.closest('button[data-resume]');
  if (!button || activeRun) return;
  button.disabled = true;
  try {
    const result = await api('/runs/'+button.dataset.resume+'/resume', {method:'POST'});
    setBusy(true);
    await monitor(result.run_id, result.conversation_id);
  } catch (error) { button.disabled = false; toast(error.message); }
});
enter().catch(()=>showLogin());

const ticketStates={draft:'待确认',submitted:'已提交',processing:'处理中',resolved:'已解决',cancelled:'已取消'};
async function refreshTickets(){
  const rows=await api('/tickets');
  $('#tickets-list').innerHTML=rows.map(t=>{
    let controls='';
    if(t.user_id===me.id&&t.status==='draft') controls='<button data-ticket-action="submit">确认提交</button> <button data-ticket-action="cancel">取消草稿</button>';
    if(me.role==='admin'&&t.status==='submitted') controls='<button data-ticket-action="accept">接单</button>';
    if(me.role==='admin'&&t.status==='processing') controls='<textarea data-ticket-note placeholder="填写处理结果" maxlength="2000"></textarea><button data-ticket-action="resolve">登记解决</button>';
    return `<article class="feedback-card" data-ticket-id="${escape(t.id)}" data-version="${t.version}"><small>${escape(t.id)} · ${escape(ticketStates[t.status]||t.status)}</small><h3>${escape(t.title)}</h3><pre>${escape(t.description)}</pre><p>${escape(t.note)}</p>${controls}</article>`;
  }).join('')||'<p>暂无工单。可在对话中说明问题并请求生成工单草稿。</p>';
}
$('#refresh-tickets').addEventListener('click',()=>refreshTickets().catch(e=>toast(e.message)));
document.addEventListener('click',async e=>{
  const button=e.target.closest('[data-ticket-action]');if(!button)return;
  const card=button.closest('[data-ticket-id]');button.disabled=true;
  try{await api('/tickets/'+card.dataset.ticketId+'/action',{method:'POST',body:{action:button.dataset.ticketAction,version:Number(card.dataset.version),note:card.querySelector('[data-ticket-note]')?.value||''}});await refreshTickets();toast('工单已更新');}catch(error){toast(error.message);}finally{button.disabled=false;}
});
