"""Small helpers for running the AI runner subprocesses."""
import threading


class StderrDrain:
    """Continuously read a subprocess's stderr on a background thread.

    The AI runners are started with stdout AND stderr piped, and the add-on
    reads stdout line by line until the process ends. If nothing reads
    stderr meanwhile, a chatty child (download progress bars from
    huggingface/torch hub, library warnings) fills the OS pipe buffer
    (only a few KB on Windows), blocks on its next stderr write, and the job
    appears frozen - e.g. a model download stuck at "5%". Draining stderr
    here prevents that; text() returns the tail for error reporting.
    """

    def __init__(self, proc, keep=20000):
        self._proc = proc
        self._keep = keep
        self._parts = []
        self._size = 0
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="StderrDrain")
        self._thread.start()

    def _run(self):
        try:
            stream = self._proc.stderr
            if stream is None:
                return
            while True:
                chunk = stream.read(1024)
                if not chunk:
                    break
                with self._lock:
                    self._parts.append(chunk)
                    self._size += len(chunk)
                    while self._size > self._keep * 2 and len(self._parts) > 1:
                        self._size -= len(self._parts.pop(0))
        except Exception:
            pass

    def text(self):
        """Everything read so far (waits briefly for the stream to finish)."""
        self._thread.join(timeout=2.0)
        with self._lock:
            data = b"" if False else "".join(
                p if isinstance(p, str) else p.decode("utf-8", "replace")
                for p in self._parts)
        return data[-self._keep:]
