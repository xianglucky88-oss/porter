"""测试级环境隔离。

在导入 app 之前把受治理定义仓库指向临时目录，避免测试写入仓库级的
`data/definitions.json`，也避免上一次运行残留的发布历史影响本次断言。
"""

from __future__ import annotations

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="porter-tests-")
os.environ.setdefault("DEFINITIONS_PATH", os.path.join(_TMP, "definitions.json"))
