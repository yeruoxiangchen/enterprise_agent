const draftAttachments = [];

function attachmentChip(name, imageUrl, remove) {
  const chip = document.createElement(imageUrl && !remove ? 'a' : 'span');
  chip.className = 'attachment-chip';
  if (imageUrl && !remove) {
    chip.href = imageUrl;
    chip.target = '_blank';
    chip.rel = 'noopener';
    chip.setAttribute('aria-label', '查看原图 ' + name);
  }
  if (imageUrl) {
    const image = document.createElement('img');
    image.src = imageUrl;
    image.alt = '';
    chip.append(image);
  }
  const label = document.createElement('span');
  label.textContent = name;
  label.title = name;
  chip.append(label);
  if (remove) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = '×';
    button.setAttribute('aria-label', '移除 ' + name);
    button.addEventListener('click', remove);
    chip.append(button);
  }
  return chip;
}

function renderDraftAttachments() {
  $('#attachments').replaceChildren(...draftAttachments.map(item =>
    attachmentChip(item.file.name, item.preview, () => {
      if (item.preview) URL.revokeObjectURL(item.preview);
      draftAttachments.splice(draftAttachments.indexOf(item), 1);
      renderDraftAttachments();
      $('#question').focus();
    })
  ));
  syncSendButton();
}

function addAttachments(files) {
  if (!files.length || $('#incident-open').disabled) return;
  if (draftAttachments.length + files.length > 8) { toast('每条消息最多可添加 8 个附件'); return; }
  for (const file of files) {
    const image = /\.(png|jpe?g|webp)$/i.test(file.name);
    const log = /\.(txt|log|md)$/i.test(file.name);
    if (!image && !log) { toast(file.name + '：请选择 PNG、JPEG、WebP 图片或 TXT、LOG、Markdown 日志'); return; }
    if (!file.size || file.size > (image ? 5 * 1024 * 1024 : 256000)) {
      toast(file.name + '：' + (file.size ? (image ? '图片最大 5 MiB' : '日志最大 256 KB') : '文件为空')); return;
    }
  }
  for (const file of files) {
    draftAttachments.push({file, preview: /\.(png|jpe?g|webp)$/i.test(file.name) ? URL.createObjectURL(file) : null});
  }
  renderDraftAttachments();
  $('#question').focus();
}

function clearAttachments() {
  for (const item of draftAttachments) if (item.preview) URL.revokeObjectURL(item.preview);
  draftAttachments.length = 0;
  $('#incident-file').value = '';
  renderDraftAttachments();
}

async function uploadAttachments(items, signal) {
  const links = [];
  for (const item of items) {
    if (!item.upload) {
      const form = new FormData();
      form.set('file', item.file);
      try {
        item.upload = await api('/incident-evidence?defer_ocr=true', {method:'POST', body:form, signal});
      } catch (error) {
        throw new Error(item.file.name + '：' + error.message);
      }
    }
    const {id, filename, kind} = item.upload;
    links.push({incident_id:id, filename, kind});
  }
  return links;
}

function appendSentAttachments(bubble, links) {
  if (!links?.length) return;
  const list = document.createElement('div');
  list.className = 'attachment-list';
  for (const item of links) {
    const url = item.kind === 'screenshot' ? '/api/incident-evidence/' + encodeURIComponent(item.incident_id) + '/image' : null;
    list.append(attachmentChip(item.filename, url));
  }
  bubble.prepend(list);
}
