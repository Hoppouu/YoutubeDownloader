import subprocess
import threading


CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class OperationCancelled(RuntimeError):
    """Raised when a cooperative background operation is cancelled."""


class CancellationToken:
    def __init__(self):
        self._event = threading.Event()

    def cancel(self):
        self._event.set()

    def is_cancelled(self):
        return self._event.is_set()

    def wait(self, timeout):
        """Wait for cancellation and return True when cancellation was requested."""
        return self._event.wait(timeout)

    def raise_if_cancelled(self):
        if self.is_cancelled():
            raise OperationCancelled("작업이 취소되었습니다.")


def request_process_termination(process):
    """Ask a subprocess to stop without blocking the GUI thread."""
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
    except OSError:
        pass


def finish_process_termination(process, timeout=2):
    """Wait for terminate and use kill only as the final subprocess fallback."""
    if process is None or process.poll() is not None:
        return
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            pass
