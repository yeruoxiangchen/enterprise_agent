"""Owned screenshot/log evidence, grounded fields and explicitly reviewed drafts."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

from .db import uid, encode


MAX_BYTES = 5 * 1024 * 1024
TEXT_TYPES = {'.txt', '.log', '.md'}
IMAGE_TYPES = {'.png', '.jpg', '.jpeg', '.webp'}
PATTERNS = {
    'error_code': re.compile(r'\b(?:HTTP\s*[45]\d{2}|(?:ERR|ERROR)_[A-Z0-9_]+|0x[0-9a-f]{4,16}|[A-Z][A-Za-z]+Error)\b', re.I),
    'environment': re.compile(r'\b(?:Windows\s*(?:10|11)|Ubuntu\s*\d{2}\.\d{2}|macOS(?:\s*\d+(?:\.\d+)*)?|Linux)\b', re.I),
    'software': re.compile(r'\b(?:VPN|SSH|Docker|Nginx|Chrome|Firefox|Python|Java|MySQL|Redis|PostgreSQL)\b', re.I),
    'symptom': re.compile(r'Permission denied|Connection refused|Connection timed out|Out of memory|'
                          r'Authentication failed|Access denied|certificate verify failed|连接超时|拒绝连接|内存不足', re.I),
}


def redact(text):
    text = re.sub(r'(?im)(\bauthorization\s*:\s*(?:bearer|basic)\s+)\S+', r'\1[REDACTED]', text)
    return re.sub(r'(?im)(\b(?:password|passwd|api[_-]?key|access[_-]?token|secret)\s*[:=]\s*)[^\s;,]+',
                  r'\1[REDACTED]', text)


def grounded_fields(lines):
    fields, seen = [], set()
    for line in lines:
        for kind, pattern in PATTERNS.items():
            for match in pattern.finditer(line['text']):
                value = match.group()
                key = (kind, value.casefold())
                if key in seen:
                    continue
                seen.add(key)
                fields.append({'kind': kind, 'value': value, 'source_line': line['number'],
                    'span': [match.start(), match.end()], 'box': line.get('box'),
                    'confidence': line.get('confidence')})
    return fields[:80]


def image_lines(path, cfg):
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
            lines.append({'number': len(lines) + 1, 'text': redact(' '.join(w['text'] for w in words)),
                          'box': [left, top, right, bottom], 'ocr_pass': source,
                          'confidence': round(sum(float(w['conf']) for w in words) / len(words), 2)})
        return lines, rows
    lines, _ = parse_tsv(process.stdout, 'primary')
    lines.sort(key=lambda line: (line['box'][1], line['box'][0]))
    for index, line in enumerate(lines): line['number'] = index + 1
    if not lines:
        raise ValueError('未识别到清晰文字，请裁剪报错区域或上传日志')
    return lines


def analyze(db, cfg, user, filename, data):
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
            from PIL import Image, ImageOps, ImageStat, UnidentifiedImageError
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
                    # Keep the original screenshot; normalize only the OCR raster.
                    raster = ImageOps.grayscale(image)
                    if ImageStat.Stat(raster).mean[0] < 127:
                        raster = ImageOps.invert(raster)
                    raster = ImageOps.autocontrast(raster)
                    ocr_input = Path(temporary)/'ocr.png'; raster.save(ocr_input, dpi=(150, 150))
                    lines = image_lines(ocr_input, cfg)
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
    fields = grounded_fields(lines)
    selected = lambda kind: list(dict.fromkeys(f['value'] for f in fields if f['kind'] == kind))
    title = ' / '.join(selected('software')[:2] + (selected('error_code') or selected('symptom'))[:1]) or '待补充的故障信息'
    description = '\n'.join(f"第{f['source_line']}行 · {f['kind']}：{f['value']}" for f in fields) or '请补充故障现象和发生环境。'
    result = {'id': ident, 'filename': filename, 'digest': hashlib.sha256(saved.read_bytes()).hexdigest(),
              'kind': 'screenshot' if dimensions else 'log', 'dimensions': dimensions,
              'lines': lines, 'fields': fields, 'suggested_title': title[:120],
              'suggested_description': description[:2600],
              'analysis_seconds': round(time.monotonic() - started, 4)}
    db.execute('INSERT INTO incident_evidence(id,user_id,payload,created_at) VALUES(?,?,?,?)',
               (ident, user['id'], encode(result), time.time()))
    return result


def owned(db, user, ident):
    row = db.one('SELECT * FROM incident_evidence WHERE id=? AND user_id=?', (ident, user['id']))
    if not row:
        raise LookupError('故障材料不存在或不属于当前用户')
    return row


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
