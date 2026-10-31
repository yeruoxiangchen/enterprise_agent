"""Stop only the dedicated business preview, after checking ownership and activity."""
import json
import os
from pathlib import Path
import signal
import sqlite3

root=Path('/data/zjr/enterprise_kb_agent')
state=json.loads((root/'artifacts/business_multi_v1_20261030/web.json').read_text())
pid=state['pid'];process=Path('/proc')/str(pid)
if not process.exists():
    print('演示网页进程已退出');raise SystemExit(0)
args=(process/'cmdline').read_bytes().split(b'\0')
if process.stat().st_uid!=os.getuid() or b'scripts/business_ops.py' not in args or b'serve' not in args:
    raise RuntimeError('PID不再属于业务演示网页，拒绝停止')
with sqlite3.connect('file:'+str(root/'preview_business_20261030/knowledge.sqlite3')+'?mode=ro',uri=True) as db:
    if db.execute("SELECT count(*) FROM runs WHERE status IN ('queued','running')").fetchone()[0]:
        raise RuntimeError('演示网页仍有问答运行，请先完成或取消')
os.kill(pid,signal.SIGTERM)
print('已请求停止独立演示网页',pid)
