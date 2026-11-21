"""Owned screenshot/log evidence, grounded fields and explicitly reviewed drafts."""
from __future__ import annotations

import csv
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

from .db import uid, encode


MAX_BYTES = 5 * 1024 * 1024
TEXT_TYPES = {'.txt', '.log', '.md'}
IMAGE_TYPES = {'.png', '.jpg', '.jpeg', '.webp'}
PATTERNS = {
    'error_code': re.compile(r'\b(?:HTTP\s*[:：]?\s*[45]\d{2}|(?:ERR|ERROR)_[A-Z0-9_]+|0x[0-9a-f]{4,16}|'
                             r'EACCES|ENOENT|ENOSPC|ECONNRESET|ETIMEDOUT|[A-Z][A-Za-z]+Error)\b', re.I | re.ASCII),
    'environment': re.compile(r'\b(?:Windows\s*(?:10|11)|Ubuntu\s*\d{2}\.\d{2}|macOS(?:\s*\d+(?:\.\d+)*)?|Linux)\b', re.I | re.ASCII),
    'software': re.compile(r'\b(?:VPN|SSH|Docker|Nginx|Chrome|Firefox|Python|Java|MySQL|Redis|PostgreSQL)\b', re.I | re.ASCII),
    'symptom': re.compile(r'Permission denied|Connection refused|Connection timed out|Out of memory|'
                          r'Authentication failed|Access denied|certificate verify failed|连接超时|拒绝连接|内存不足', re.I),
}


def redact(text):
    text = re.sub(r'(?im)(\bauthorization\s*:\s*(?:bearer|basic)\s+)\S+', r'\1[REDACTED]', text)
    return re.sub(r'(?im)(\b(?:password|passwd|api[_-]?key|access[_-]?token|secret)\s*[:=]\s*)[^\s;,]+',
                  r'\1[REDACTED]', text)


def grounded_fields(lines):
    fields = {}
    for line in lines:
        for kind, pattern in PATTERNS.items():
            for match in pattern.finditer(line['text']):
                value = match.group()
                key = (kind, value.casefold())
                scores = [c for a, b, c in line.get('word_scores', ())
                          if a < match.end() and b > match.start()]
                confidence = min(scores) if scores else line.get('confidence')
                if key in fields and (confidence or 0) <= (fields[key]['confidence'] or 0):
                    continue
                fields[key] = {'kind': kind, 'value': value, 'source_line': line['number'],
                    'span': [match.start(), match.end()], 'box': line.get('box'),
                    'confidence': confidence}
    return list(fields.values())[:80]


# Both OCR engines report percentages. Only uncertainty about the requested
# detail or the fault itself warrants interrupting an investigation.
OCR_READABLE = 80
FIELD_LABELS = {'error_code': '错误码', 'symptom': '报错原文或故障现象',
                'environment': '操作系统及版本', 'software': '软件名称'}
FIELD_QUESTIONS = {
    'error_code': re.compile(r'错误码|状态码|报错码|error code|status code|HTTP\s*(?:码|代码)', re.I),
    'environment': re.compile(r'操作系统|系统版本|适用.*命令|哪个系统|哪种系统|系统.*命令', re.I),
    'software': re.compile(r'哪个软件|什么软件|软件名称|哪个应用', re.I),
}
FAULT_TEXT = re.compile(
    r'\b(?:cannot|unable to|could not|not found|invalid|exception|failed|denied|refused|timed out|'
    r'unreachable|unavailable|unauthorized|forbidden|expired|locked|missing)\b'
    r'|\b(?:error|fatal|panic)\s*[:：]\s*\S|(?:报错|错误)\s*[:：]\s*\S|失败|打不开|超时|拒绝|不足'
    r'|找不到|未找到|不兼容|未连接|已断开|崩溃|闪退|白屏|黑屏|卡死|无响应|未授权|已过期|被锁定|空间已满'
    r'|(?:无法|不能|未能)(?:登录|启动|访问|联网|连接|打开|安装|运行|加载|上传|下载|同步)', re.I | re.ASCII)
PARTIAL_HTTP = re.compile(r'\bHTTP\s*[:：]?\s*[45][0-9OoIl?？]{1,2}(?![a-z0-9])', re.I | re.ASCII)
UNKNOWN_REPLY = re.compile(r'^(?:我?(?:也|暂时)?(?:不清楚|不知道|看不清(?:楚)?|不确定|无法辨认)|unknown)[。.!！]?$', re.I)
SUPPORT_ADVICE = re.compile(
    r'排查|排障|故障处理|解决办法|恢复访问|(?:怎么|如何|怎样|应该|先).{0,12}(?:处理|修复|解决|恢复|检查)'
    r'|\btroubleshoot\b|\bhow (?:do I|to|can I) (?:fix|restore|resolve)\b', re.I)
GENERIC_FAILURE = re.compile(r'(?:(?:request|operation|action) failed|(?:请求|操作|执行)失败)[.!。！]?', re.I)
MISSING_CODE_MENTION = re.compile(r'(?:没(?:有|显示|看到)|看不清|不知道)(?:具体的?)?(?:错误码|状态码|报错码)'
                                  r'|(?:错误码|状态码|报错码)(?:看不清|没显示|不清楚|不知道)')


def requests_support_advice(question):
    if re.search(r'(?:不必|不用|不要|无需)(?:再)?(?:查|检索|参考|排查)|只(?:需|要)?(?:转录|读图|识别)', question):
        return False
    return bool(SUPPORT_ADVICE.search(question))


def incident_observations(item):
    fields = list(item['fields'])
    fault_lines = {f['source_line'] for f in fields if f['kind'] in ('error_code', 'symptom')}
    for line in item['lines']:
        partial = PARTIAL_HTTP.search(line['text'])
        if partial and not PATTERNS['error_code'].fullmatch(partial.group()):
            fields.append({'kind': 'error_code', 'value': partial.group(),
                           'source_line': line['number'], 'illegible': True})
            fault_lines.add(line['number'])
        if line['number'] not in fault_lines and FAULT_TEXT.search(line['text']):
            fields.append({'kind': 'symptom', 'value': line['text'][:180],
                           'source_line': line['number'], 'confidence': line.get('confidence')})
    return [{'kind': f['kind'], 'value': f['value'], 'source_line': f['source_line'],
             'certain': not f.get('illegible') and (item['kind'] == 'log' or
                         (f.get('confidence') or 0) >= OCR_READABLE)} for f in fields]


def diagnosis_review(incidents, question, replies):
    """Decide once for the question, using all attachments and employee-supplied details."""
    items = [{'id': item['id'], 'filename': item['filename'], 'observed': incident_observations(item),
              'answers': replies[item['id']]} for item in incidents]
    diagnosis = {'question': question, 'observations': items}
    supplied = {k for k, pattern in PATTERNS.items() if pattern.search(question)}
    if FAULT_TEXT.search(question):
        supplied.add('symptom')
    if re.search(r'还是|不确定|看不清|\bor\b', question, re.I | re.ASCII):
        supplied -= {k for k, p in FIELD_QUESTIONS.items() if p.search(question)}
        if len(set(PATTERNS['error_code'].findall(question))) > 1:
            supplied.discard('error_code')
    known = supplied | {k for item in items for k in item['answers']}
    known.update(f['kind'] for item in items for f in item['observed']
                 if f['certain'] and not GENERIC_FAILURE.fullmatch(f['value'].strip()))
    intent = MISSING_CODE_MENTION.sub('', question)
    requested = [k for k, p in FIELD_QUESTIONS.items() if p.search(intent)]
    missing = next((k for k in requested if k not in known), None)
    if not missing and not requested and not known.intersection(('error_code', 'symptom')):
        unclear_code = any(f['kind'] == 'error_code' for item in items for f in item['observed'])
        missing = 'error_code' if unclear_code and intent == question else 'symptom'
    if missing:
        item = next((i for i in items if any(f['kind'] == missing for f in i['observed'])), items[0])
        candidates = list(dict.fromkeys(f['value'] for f in item['observed'] if f['kind'] == missing))[:3]
        hint = '（识别为' + ' / '.join(candidates) + '）' if candidates else ''
        return diagnosis, {'incident_id': item['id'], 'field': missing,
                           'prompt': f'请补充{FIELD_LABELS[missing]}{hint}，以便选择排查步骤。'}
    return diagnosis, None


def reliable_readings(item):
    return [f for f in item['observed'] if f['certain'] and f['kind'] not in item['answers']]


def prepare_diagnosis(db, cfg, run_id):
    """Claim a queued intake, publish its readings, then dispatch through the ordinary agent."""
    last = db.one("SELECT id,kind,payload FROM events WHERE run_id=? AND kind IN "
                  "('incident_request','attachment_request','incident_diagnosis') ORDER BY id DESC LIMIT 1", (run_id,))
    if not last or last['kind'] == 'incident_diagnosis':
        return False
    request = json.loads(last['payload'])
    if not db.execute("UPDATE runs SET status='running',updated_at=? WHERE id=? AND status='queued' "
                      "AND cancel_requested=0 AND (SELECT MAX(id) FROM events WHERE run_id=? AND kind IN "
                      "('incident_request','attachment_request','incident_diagnosis'))=?", (time.time(),run_id,run_id,last['id'])):
        return True
    db.event(run_id, 'incident_worker_started', {})
    error, prompt, diagnosis = None, None, None
    try:
        run = db.one('SELECT user_id FROM runs WHERE id=?',(run_id,))
        links = db.all('SELECT incident_id,digest FROM run_incidents WHERE run_id=? ORDER BY rowid',(run_id,))
        if set(request['answers']) != {link['incident_id'] for link in links} or not links:
            raise ValueError('附件已变更，请重新上传后排查。')
        items = []
        for link in links:
            item = recognize(db,cfg,{'id':run['user_id']},link['incident_id'])
            if item['digest'] != link['digest']:
                raise ValueError('附件已变更，请重新上传后排查。')
            items.append(item)
        diagnosis, prompt = diagnosis_review(items, request['question'], request['answers'])
        if last['kind'] == 'attachment_request':
            prompt = None
        question = request['question']
        supplied = [f'{FIELD_LABELS[k]}：{v}' for answers in request['answers'].values() for k,v in answers.items()]
        if supplied:
            question += '\n员工补充：' + '；'.join(supplied)
        if cfg.agent_policy != 'business_native_v1':
            terms = [f['value'] for item in diagnosis['observations'] for f in reliable_readings(item)]
            if not terms and last['kind'] == 'attachment_request':
                terms = [line['text'] for item in items for line in item['lines'][:8]]
                diagnosis['attachment_text'] = ' '.join(terms)[:800]
            if terms:
                question += '\n材料识别：' + '；'.join(dict.fromkeys(terms))[:800]
    except (ValueError, LookupError, OSError) as exc:
        error = str(exc) if isinstance(exc, ValueError) else '故障材料读取失败，请重新上传。'
    except Exception:
        import logging
        logging.exception('Failed to read incident for run %s',run_id)
        error = '故障材料读取失败，请稍后重试。'
    now = time.time()
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        def event(kind,payload):
            c.execute('INSERT INTO events(run_id,kind,payload,created_at) VALUES(?,?,?,?)',(run_id,kind,encode(payload),now))
        if c.execute('SELECT cancel_requested FROM runs WHERE id=?',(run_id,)).fetchone()[0]:
            c.execute("UPDATE runs SET status='cancelled',updated_at=? WHERE id=?",(now,run_id))
            event('cancelled',{})
            return True
        if error:
            c.execute("UPDATE runs SET status='error',error=?,updated_at=? WHERE id=?", (error,now,run_id))
            event('error', {'message':error})
            return True
        if prompt:
            diagnosis['clarification'] = prompt
        event('incident_diagnosis', diagnosis)
        if prompt:
            c.execute("UPDATE runs SET status='clarify',answer=?,updated_at=? WHERE id=?",(prompt['prompt'],now,run_id))
            return True
        # Each existing agent runtime atomically claims its input from queued.
        # Persisting the review first also makes this hand-off restartable.
        c.execute("UPDATE runs SET status='queued',question=?,updated_at=? WHERE id=?",(question,now,run_id))
    return False


def _tesseract_lines(path, cfg):
    binary = Path(cfg.incident_ocr_bin)
    if not binary.is_file():
        raise ValueError('截图文字识别尚未配置，请先上传文字日志')
    prefix = binary.parent.parent
    data_dirs = sorted((prefix/'share/tesseract-ocr').glob('*/tessdata'))
    if not data_dirs:
        raise ValueError('OCR语言文件尚未安装')
    env = {**os.environ, 'OMP_THREAD_LIMIT': '2',
           'LD_LIBRARY_PATH': str(prefix/'lib/x86_64-linux-gnu') + ':' + os.environ.get('LD_LIBRARY_PATH', '')}
    command = [str(binary), str(path), 'stdout', '--tessdata-dir', str(data_dirs[-1]),
               '-l', cfg.incident_ocr_languages, '--psm', '6', 'tsv']
    try:
        process = subprocess.run(command, capture_output=True, text=True, timeout=20, env=env)
    except subprocess.TimeoutExpired:
        raise ValueError('截图识别超时，请裁剪到主要报错区域后重试') from None
    if process.returncode:
        raise ValueError('截图识别失败，请检查OCR组件或改为上传文字日志')
    def parse_tsv(content, source):
        grouped = {}
        rows = list(csv.DictReader(io.StringIO(content), delimiter='\t'))
        for row in rows:
            if row.get('level') != '5' or not row.get('text', '').strip() or float(row['conf']) < 15:
                continue
            key = tuple(row[k] for k in ['page_num', 'block_num', 'par_num', 'line_num'])
            grouped.setdefault(key, []).append(row)
        lines = []
        for words in grouped.values():
            left = min(int(w['left']) for w in words); top = min(int(w['top']) for w in words)
            right = max(int(w['left']) + int(w['width']) for w in words)
            bottom = max(int(w['top']) + int(w['height']) for w in words)
            raw = ' '.join(w['text'] for w in words)
            text = redact(raw)
            scores = []
            if text == raw:
                offset = 0
                for w in words:
                    end = offset + len(w['text'])
                    scores.append((offset, end, float(w['conf'])))
                    offset = end + 1
            lines.append({'number': len(lines) + 1, 'text': text, 'word_scores': scores,
                          'box': [left, top, right, bottom], 'ocr_pass': source,
                          'confidence': round(sum(float(w['conf']) for w in words) / len(words), 2)})
        return lines, rows
    lines, _ = parse_tsv(process.stdout, 'primary')
    lines.sort(key=lambda line: (line['box'][1], line['box'][0]))
    for index, line in enumerate(lines): line['number'] = index + 1
    return lines


def image_lines(path, cfg):
    """Prefer detected text regions; retain the installed legacy fallback."""
    backend=cfg.incident_ocr_backend
    if backend not in {'auto','rapidocr','tesseract'}:
        raise ValueError('未知的截图识别后端')
    packages=Path(cfg.incident_ocr_packages)
    installed=(packages/'READY.json').is_file()
    if backend=='rapidocr' and not installed:
        raise ValueError('截图文字识别组件尚未安装，请运行setup_rapidocr.sh')
    if backend=='rapidocr' or (backend=='auto' and installed):
        env={**os.environ,'CUDA_VISIBLE_DEVICES':'','PYTHONDONTWRITEBYTECODE':'1',
             'OMP_NUM_THREADS':'2','OPENBLAS_NUM_THREADS':'1','MKL_NUM_THREADS':'1'}
        try:
            process=subprocess.run([sys.executable,'-B',str(Path(__file__).with_name('incident_ocr_worker.py')),
                '--image',str(path),'--packages',str(packages)],capture_output=True,text=True,timeout=20,env=env)
        except subprocess.TimeoutExpired:
            raise ValueError('截图识别超时，请裁剪到主要报错区域后重试')from None
        if process.returncode:
            raise ValueError('截图识别失败，请检查CPU OCR组件或上传文字日志')
        try:
            lines=json.loads(process.stdout)
        except json.JSONDecodeError:
            raise ValueError('截图识别结果无效，请检查CPU OCR组件')from None
        for line in lines:
            line['text']=redact(line['text'])
        return lines
    from PIL import Image,ImageOps,ImageStat
    with Image.open(path)as image:
        raster=ImageOps.grayscale(image)
        if ImageStat.Stat(raster).mean[0]<127:
            raster=ImageOps.invert(raster)
        raster=ImageOps.autocontrast(raster)
        with tempfile.TemporaryDirectory(dir=cfg.root/'tmp',prefix='legacy-ocr-')as temporary:
            legacy=Path(temporary)/'ocr.png';raster.save(legacy,dpi=(150,150))
            return _tesseract_lines(legacy,cfg)


def analyze(db, cfg, user, filename, data, defer_ocr=False):
    filename = Path(filename.replace('\\', '/')).name[:120]
    suffix = Path(filename).suffix.lower()
    if suffix not in TEXT_TYPES | IMAGE_TYPES:
        raise ValueError('支持 PNG、JPEG、WebP 截图，以及 TXT、LOG、Markdown 日志')
    if not data or len(data) > MAX_BYTES:
        raise ValueError('文件需为1字节至5 MiB')
    started = time.monotonic()
    ident = uid()
    storage = cfg.root/'uploads/incidents'; storage.mkdir(parents=True, exist_ok=True, mode=0o700)
    saved = storage/(ident + ('.png' if suffix in IMAGE_TYPES else '.txt'))
    dimensions = None
    if suffix in IMAGE_TYPES:
        try:
            from PIL import Image, UnidentifiedImageError
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in {'PNG', 'JPEG', 'WEBP'}:
                    raise ValueError('图片内容需为PNG、JPEG或WebP')
                if image.width * image.height > 12_000_000 or image.width < 10 or image.height < 10:
                    raise ValueError('截图尺寸需在10像素以上且不超过1200万像素')
                image = image.convert('RGB')
                image.thumbnail((2400, 2400))
                dimensions = [image.width, image.height]
                with tempfile.TemporaryDirectory(dir=cfg.root/'tmp', prefix='incident-') as temporary:
                    normalized = Path(temporary)/'image.png'; image.save(normalized)
                    # The region detector sees the source colors and original
                    # line positions; the legacy backend handles its own raster.
                    lines = [] if defer_ocr else image_lines(normalized, cfg)
                    saved.write_bytes(normalized.read_bytes())
        except ImportError:
            raise ValueError('截图处理组件尚未安装，请先上传文字日志') from None
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            raise ValueError('图片内容无效或尺寸过大') from None
    else:
        if len(data) > 256_000 or b'\0' in data:
            raise ValueError('文字日志需为256 KB以内的文本文件')
        try:
            text = data.decode('utf-8-sig')
        except UnicodeDecodeError:
            try: text = data.decode('gb18030')
            except UnicodeDecodeError: raise ValueError('日志需使用UTF-8或GB18030编码') from None
        text = redact(text)
        lines = [{'number': i + 1, 'text': line, 'confidence': None, 'box': None}
                 for i, line in enumerate(text.splitlines()) if line.strip()]
        if not lines or len(lines) > 3000:
            raise ValueError('日志需包含有效文字且不超过3000行')
        saved.write_text(text)
    saved.chmod(0o600)
    result = {'id': ident, 'filename': filename, 'digest': hashlib.sha256(saved.read_bytes()).hexdigest(),
              'kind': 'screenshot' if dimensions else 'log', 'dimensions': dimensions,
              'ocr_pending': bool(dimensions and defer_ocr),
              'analysis_seconds': round(time.monotonic() - started, 4)}
    result.update(recognized_fields(lines))
    db.execute('INSERT INTO incident_evidence(id,user_id,payload,created_at) VALUES(?,?,?,?)',
               (ident, user['id'], encode(result), time.time()))
    return result


def recognized_fields(lines):
    fields = grounded_fields(lines)
    selected = lambda kind: list(dict.fromkeys(f['value'] for f in fields if f['kind'] == kind))
    title = ' / '.join(selected('software')[:2] + (selected('error_code') or selected('symptom'))[:1]) or '待补充的故障信息'
    description = '\n'.join(f"第{f['source_line']}行 · {f['kind']}：{f['value']}" for f in fields) or '请补充故障现象和发生环境。'
    return {'lines':lines, 'fields':fields, 'suggested_title':title[:120], 'suggested_description':description[:2600]}


def recognize(db, cfg, user, ident):
    result = json.loads(owned(db,user,ident)['payload'])
    if not result.get('ocr_pending'):
        return result
    path = cfg.root/'uploads/incidents'/(ident+'.png')
    with path.open('rb') as image:
        # A draft preview and a worker can ask for the same file concurrently.
        fcntl.flock(image, fcntl.LOCK_EX)
        result = json.loads(owned(db,user,ident)['payload'])
        if result.get('ocr_pending'):
            started = time.monotonic()
            result.update(recognized_fields(image_lines(path,cfg)))
            result['ocr_pending'] = False
            result['analysis_seconds'] = round(result['analysis_seconds'] + time.monotonic()-started,4)
            db.execute('UPDATE incident_evidence SET payload=? WHERE id=? AND user_id=?',
                       (encode(result),ident,user['id']))
    return result


def owned(db, user, ident):
    row = db.one('SELECT * FROM incident_evidence WHERE id=? AND user_id=?', (ident, user['id']))
    if not row:
        raise LookupError('故障材料不存在或不属于当前用户')
    return row


def message_attachments(db, user, *, conversation_id=None, run_id=None):
    """Return display metadata only for messages and files owned by the reader."""
    scope = 'r.id=?' if run_id is not None else 'r.conversation_id=?'
    grouping = '' if run_id is not None else 'r.id AS run_id,'
    return db.all(
        f"SELECT {grouping} i.incident_id,i.digest,json_extract(e.payload,'$.filename') AS filename,"
        "json_extract(e.payload,'$.kind') AS kind FROM run_incidents i JOIN runs r ON r.id=i.run_id "
        "JOIN incident_evidence e ON e.id=i.incident_id AND e.user_id=r.user_id "
        f"WHERE r.user_id=? AND {scope} ORDER BY i.rowid",
        (user['id'], run_id if run_id is not None else conversation_id))


def confirm_draft(db, user, ident, digest, title, description, confirmed):
    title, description = title.strip(), description.strip()
    if not confirmed:
        raise ValueError('请先核对提取结果并确认草稿内容')
    if not title or len(title) > 120 or not description or len(description) > 2700:
        raise ValueError('标题需1–120字，问题描述需1–2700字')
    with db.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        row = connection.execute('SELECT * FROM incident_evidence WHERE id=? AND user_id=?', (ident, user['id'])).fetchone()
        if not row:
            raise LookupError('故障材料不存在或不属于当前用户')
        evidence = json.loads(row['payload'])
        if evidence.get('ocr_pending'):
            raise ValueError('请先读取并核对故障信息')
        if evidence['digest'] != digest:
            raise ValueError('材料版本已变化，请重新核对')
        full_description = description + f'\n\n故障材料：{ident}；SHA256：{digest}'
        if row['run_id']:
            ticket = dict(connection.execute('SELECT * FROM service_tickets WHERE source_run=?', (row['run_id'],)).fetchone())
            if ticket['title'] != title or ticket['description'] != full_description:
                raise ValueError('此材料已有不同内容的草稿，请从工单页面处理')
            return ticket
        now = time.time(); conversation, run, ticket = uid(), uid(), uid()
        state = encode({'incident_evidence_id': ident, 'source_digest': digest, 'user_confirmed': True,
                        'model_generated': False})
        connection.execute('INSERT INTO conversations VALUES(?,?,?,?)', (conversation, user['id'], title[:45], now))
        connection.execute("INSERT INTO runs(id,conversation_id,user_id,question,base_ids,mode,status,state,answer,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'incident','answered',?,?,?,?)",
            (run, conversation, user['id'], '根据已核对的故障材料准备工单草稿：' + title, '[]', state,
             '已根据你核对的故障信息生成草稿，请在工单页面确认提交。', now, now))
        connection.execute('INSERT INTO service_tickets VALUES(?,?,?,?,?,?,?,?,?,?)',
            (ticket, user['id'], run, title, full_description, 'draft', '', 1, now, now))
        connection.execute('UPDATE incident_evidence SET run_id=? WHERE id=?', (run, ident))
        connection.execute('INSERT INTO events(run_id,kind,payload,created_at) VALUES(?,?,?,?)',
            (run, 'incident_draft_confirmed', state, now))
        return dict(connection.execute('SELECT * FROM service_tickets WHERE id=?', (ticket,)).fetchone())
