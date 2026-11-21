let incidentEvidence = null;
let incidentVersion = 0;
document.addEventListener('reset-incident', () => {
  incidentVersion++;
  incidentEvidence = null;
  $('#incident-file').value = '';
  $('#incident-file').disabled = false;
  $('#incident-progress').textContent = '';
  $('#incident-reviewed').checked = false;
  $('#incident-result').classList.add('hidden');
  $('#incident-dialog').close();
});
document.querySelector('#incident-open').addEventListener('click', () => {
  $('#incident-query').value = $('#question').value;
  document.querySelector('#incident-dialog').showModal();
});
function showIncident(result) {
  incidentEvidence = result;
  const pending = result.ocr_pending;
  document.querySelector('#incident-picture').classList.toggle('hidden', result.kind !== 'screenshot');
  if (result.kind === 'screenshot') {
    document.querySelector('#incident-image').src = '/api/incident-evidence/'+result.id+'/image';
    const regions = document.querySelector('#incident-regions');
    regions.setAttribute('viewBox', '0 0 '+result.dimensions.map(Number).join(' '));
    regions.innerHTML = result.fields.filter(field => field.box).map(field => {
      const [left, top, right, bottom] = field.box.map(Number);
      return `<rect x="${left}" y="${top}" width="${right-left}" height="${bottom-top}" fill="none" stroke="#dc6b35" stroke-width="2"/>`;
    }).join('');
  }
  const kinds = incidentLabels;
  document.querySelector('#incident-fields').innerHTML = result.fields.map(field =>
    `<p><b>${escape(kinds[field.kind])}</b> ${escape(field.value)} <small>第${field.source_line}行${field.confidence == null ? '' : ' · OCR '+field.confidence.toFixed(0)+'%'}</small></p>`
  ).join('') || '<p>未提取到结构化字段，可在下方查看识别原文。</p>';
  document.querySelector('#incident-lines').textContent = result.lines.map(line => `${line.number}  ${line.text}`).join('\n');
  document.querySelector('#incident-title').value = result.suggested_title;
  document.querySelector('#incident-description').value = result.fields.length ? result.fields.map(field =>
    `第${field.source_line}行 · ${kinds[field.kind]}：${field.value}`).join('\n').slice(0,2700) : result.suggested_description;
  document.querySelector('#incident-progress').textContent = `${result.filename} · 系统识别结果`;
  $('#incident-readings').classList.toggle('hidden',pending);
  $('#incident-reviewed').disabled = pending;
  $('#incident-draft').disabled = pending;
  if (pending) $('#incident-progress').textContent = result.filename+' · 已上传，可以发送问题。';
  $('#incident-result').classList.remove('hidden');
}
document.querySelector('#incident-file').addEventListener('change', async event => {
  const file = event.target.files[0];
  if (!file) return;
  const version = ++incidentVersion;
  incidentEvidence = null;
  clearIncidentFollowup();
  $('#incident-ticket-options').open = false;
  document.querySelector('#incident-result').classList.add('hidden');
  document.querySelector('#incident-reviewed').checked = false;
  document.querySelector('#incident-progress').textContent = '正在上传故障材料…';
  event.target.disabled = true;
  try {
    const form = new FormData(); form.set('file', file);
    const result = await api('/incident-evidence?defer_ocr=true', {method:'POST', body:form});
    if (version !== incidentVersion) return;
    showIncident(result);
  } catch (error) {
    if (version === incidentVersion) document.querySelector('#incident-progress').textContent = error.message;
  } finally { if (version === incidentVersion) event.target.disabled = false; }
});
$('#incident-ticket-options').addEventListener('toggle', async event => {
  if (!event.target.open || !incidentEvidence?.ocr_pending) return;
  const id = incidentEvidence.id, version = incidentVersion;
  $('#incident-progress').textContent = '正在读取草稿所需的故障信息…';
  try {
    const ready = await api('/incident-evidence/'+id+'/recognize', {method:'POST'});
    if (version === incidentVersion) showIncident(ready);
  } catch (error) {
    if (version === incidentVersion) $('#incident-progress').textContent = error.message;
  }
});
document.querySelector('#incident-question').addEventListener('click', () => {
  if (!incidentEvidence || $('#send').disabled) return;
  clearIncidentFollowup();
  pendingIncident = {id:incidentEvidence.id};
  $('#question').value = $('#incident-query').value.trim() || '请根据知识库帮我排查这份材料中的故障。';
  document.querySelector('#incident-dialog').close();
  tab('chat');
  $('#question-form').requestSubmit();
});
document.querySelector('#incident-draft').addEventListener('click', async event => {
  if (!incidentEvidence || !document.querySelector('#incident-reviewed').checked) {
    toast('请先核对并确认故障信息'); return;
  }
  event.target.disabled = true;
  try {
    const ticket = await api('/incident-evidence/'+incidentEvidence.id+'/draft', {method:'POST', body:{
      digest:incidentEvidence.digest, confirmed:true,
      title:document.querySelector('#incident-title').value,
      description:document.querySelector('#incident-description').value
    }});
    document.querySelector('#incident-dialog').close();
    await tab('tickets'); await refreshHistory();
    toast('已生成待确认草稿：'+ticket.id);
  } catch (error) { toast(error.message); }
  finally { event.target.disabled = false; }
});
