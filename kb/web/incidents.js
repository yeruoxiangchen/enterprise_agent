let incidentEvidence = null;
document.querySelector('#incident-open').addEventListener('click', () => {
  document.querySelector('#incident-dialog').showModal();
});
document.querySelector('#incident-file').addEventListener('change', async event => {
  const file = event.target.files[0];
  if (!file) return;
  incidentEvidence = null;
  document.querySelector('#incident-result').classList.add('hidden');
  document.querySelector('#incident-reviewed').checked = false;
  document.querySelector('#incident-progress').textContent = '正在提取故障信息…';
  event.target.disabled = true;
  try {
    const form = new FormData(); form.set('file', file);
    const result = await api('/incident-evidence', {method:'POST', body:form});
    incidentEvidence = result;
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
    const kinds = {error_code:'错误码', environment:'运行环境', software:'软件', symptom:'故障现象'};
    document.querySelector('#incident-fields').innerHTML = result.fields.map(field =>
      `<p><b>${escape(kinds[field.kind])}</b> ${escape(field.value)} <small>第${field.source_line}行${field.confidence == null ? '' : ' · OCR '+field.confidence.toFixed(0)+'%'}</small></p>`
    ).join('') || '<p>没有找到明确的故障字段，请根据原始材料补充描述。</p>';
    document.querySelector('#incident-lines').textContent = result.lines.map(line => `${line.number}  ${line.text}`).join('\n');
    document.querySelector('#incident-title').value = result.suggested_title;
    document.querySelector('#incident-description').value = result.fields.length ? result.fields.map(field =>
      `第${field.source_line}行 · ${kinds[field.kind]}：${field.value}`).join('\n').slice(0,2700) : result.suggested_description;
    document.querySelector('#incident-progress').textContent = `${result.filename} · 已提取 ${result.fields.length} 项信息，请核对。`;
    document.querySelector('#incident-result').classList.remove('hidden');
  } catch (error) {
    document.querySelector('#incident-progress').textContent = error.message;
  } finally { event.target.disabled = false; }
});
document.querySelector('#incident-question').addEventListener('click', () => {
  if (!incidentEvidence || !document.querySelector('#incident-reviewed').checked) {
    toast('请先核对并确认故障信息'); return;
  }
  // Only grounded field values are carried into a fresh, explicit read request.
  const details = incidentEvidence.fields.map(field => field.value).join('；');
  if (!details) { toast('未找到明确字段，请在提问框补充故障现象'); return; }
  document.querySelector('#question').value = '请根据知识库帮助我排查以下故障：' + details;
  pendingIncidentIds = [incidentEvidence.id];
  document.querySelector('#incident-dialog').close();
  tab('chat'); document.querySelector('#question').focus();
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
