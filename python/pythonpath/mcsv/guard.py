import hashlib
import os
import tempfile
from pathlib import Path

from .legacy import FormatError


class ConflictError(FormatError):
    pass


class SaveLock:
    def __init__(self, path):
        identity = os.path.normcase(str(Path(path).resolve())).encode("utf-8")
        cache = Path(tempfile.gettempdir()) / (
            "mcsv-locks-" + (str(os.getuid()) if hasattr(os, "getuid") else "user")
        )
        if cache.is_symlink():
            raise FormatError("Недопустимый каталог системных блокировок.")
        cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = cache / (hashlib.sha256(identity).hexdigest() + ".lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or path.parent.is_symlink():
            raise FormatError("Недопустимый путь блокировки набора.")
        self.file = open(path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                if path.stat().st_size == 0:
                    self.file.write(b"0")
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            self.file = None
            raise ConflictError(
                "Этот набор сейчас сохраняется в другом окне или процессе. Повторите сохранение после его завершения."
            ) from exc

    def release(self):
        if self.file is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        finally:
            self.file.close()
            self.file = None
