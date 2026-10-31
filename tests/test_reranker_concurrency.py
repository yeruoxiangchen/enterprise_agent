import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from kb.reranking import Worker


def test_concurrent_queries_keep_their_own_scores():
    class Pipe:
        latest=None
        def write(self,payload):self.latest=json.loads(payload)
        def flush(self):pass
    pipe=Pipe();worker=Worker.__new__(Worker)
    worker.lock=threading.RLock();worker.process=SimpleNamespace(stdin=pipe)
    def receive():
        time.sleep(.01)
        return {'scores':[float(pipe.latest['query'])]}
    worker.receive=receive
    with ThreadPoolExecutor(max_workers=6) as pool:
        scores=list(pool.map(lambda i:worker.score(str(i),['document']),range(12)))
    assert scores==[[float(i)] for i in range(12)]
