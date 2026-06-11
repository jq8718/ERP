from __future__ import annotations

import sys
from logging.handlers import RotatingFileHandler


class SafeRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler variant tolerant of transient Windows file locks."""

    def doRollover(self) -> None:
        try:
            super().doRollover()
        except OSError as exc:
            print(f"日志轮转暂时失败，将继续写入当前日志文件：{exc}", file=sys.stderr)
            if self.stream is None and not self.delay:
                self.stream = self._open()
