import logging
import threading
from .agent import Harness
from .ingestion import ingest


class Workers:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings
        self.stop_event = threading.Event()
        self.run_ready = threading.Event()
        self.threads = []

    def start(self):
        self.db.execute("UPDATE documents SET status='queued' WHERE status='processing'")
        self.db.execute("UPDATE runs SET status='queued' WHERE status='running'")
        for kind in ("documents", "runs"):
            thread = threading.Thread(target=self.loop, args=(kind,), daemon=True, name=f"kb-{kind}")
            thread.start()
            self.threads.append(thread)

    def notify_run(self):
        self.run_ready.set()

    def loop(self, kind):
        while not self.stop_event.is_set():
            try:
                if kind == 'runs':
                    self.run_ready.clear()
                row = self.db.one(f"SELECT id FROM {kind} WHERE status='queued' ORDER BY created_at LIMIT 1")
                if row:
                    if kind == "documents":
                        ingest(self.db, self.settings, row["id"])
                    else:
                        Harness(self.db, self.settings).execute(row["id"])
                elif kind == 'runs':
                    if not self.stop_event.is_set():
                        self.run_ready.wait(15)  # Also recover work enqueued by an external process.
                else:
                    self.stop_event.wait(.35)
            except Exception:
                logging.exception("Knowledge worker failed")
                self.stop_event.wait(1)

    def stop(self):
        self.stop_event.set()
        self.run_ready.set()
        for thread in self.threads:
            thread.join(timeout=3)
