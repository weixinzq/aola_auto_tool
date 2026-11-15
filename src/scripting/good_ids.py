"""Per-process cache for the double battle script's editable role ID list."""
import json
import logging
from pathlib import Path
from time import monotonic


class HotGoodIds:
    def __init__(self, path: Path):
        self.path = path
        self._ids = None
        self._signature = None
        self._next_check = 0.0
        self._last_error = None

    def get(self, bad_ids: set[int]) -> frozenset[int]:
        now = monotonic()
        if self._ids is not None and now < self._next_check:
            return self._ids
        self._next_check = now + 1.0
        try:
            stat = self.path.stat()
            signature = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
            if signature != self._signature:
                values = json.loads(self.path.read_text(encoding="utf-8-sig"))
                if not isinstance(values, list) or any(type(x) is not int or x <= 0 for x in values):
                    raise ValueError("good_ids 必须是正整数角色 ID 的 JSON 数组")
                ids = frozenset(values)
                if ids & bad_ids:
                    raise ValueError("bad_ids 与 good_ids 不能有交集")
                self._ids, self._signature = ids, signature
                self._last_error = None
                logging.getLogger(__name__).info("已更新 good_ids：%s，共 %d 个 ID", self.path, len(ids))
        except (OSError, ValueError) as error:
            if self._ids is None:
                raise ValueError(f"无法加载 good_ids：{self.path}；{error}") from error
            message = str(error)
            if message != self._last_error:
                logging.getLogger(__name__).warning("good_ids 更新失败，继续使用旧名单：%s", message)
                self._last_error = message
        return self._ids
