import io
from types import SimpleNamespace
from kb import reranking


def test_startup_timeout_does_not_change_request_timeout(monkeypatch,tmp_path):
    (tmp_path/'logs').mkdir()
    seen=[]
    stdout=io.StringIO('{"ready":true}\n{"scores":[.2]}\n'.replace('.2','0.2'))
    process=SimpleNamespace(stdout=stdout,poll=lambda:None,stdin=io.StringIO())
    monkeypatch.setattr(reranking.subprocess,'Popen',lambda *a,**kw:process)
    monkeypatch.setattr(reranking.atexit,'register',lambda *a:None)
    def ready(r,w,e,t):seen.append(t);return (r,[],[])
    monkeypatch.setattr(reranking.select,'select',ready)
    monkeypatch.setenv('KB_RERANKER_START_TIMEOUT','600')
    worker=reranking.Worker(SimpleNamespace(root=tmp_path,reranker_python='python',reranker_path='model'))
    assert worker.score('q',['doc'])==[.2]
    assert seen==[600,180]
    worker.log.close()


def test_dead_worker_is_replaced_on_next_query_not_silently_fallback(monkeypatch):
    cfg=SimpleNamespace(reranker_python='python',reranker_path='model')
    closed=[]
    dead=SimpleNamespace(process=SimpleNamespace(poll=lambda:1),close=lambda:closed.append(True))
    fresh=SimpleNamespace(process=SimpleNamespace(poll=lambda:None),score=lambda q,docs:[.2])
    monkeypatch.setattr(reranking,'_workers',{('python','model'):dead})
    monkeypatch.setattr(reranking,'Worker',lambda cfg:fresh)
    rows,_=reranking.rerank(cfg,'q',[{'filename':'f','text':'t','id':'c'}])
    assert closed==[True] and rows[0]['rerank_score']==.2
