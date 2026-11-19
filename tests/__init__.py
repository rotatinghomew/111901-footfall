"""让 ``python -m unittest discover -s tests`` 直接可用，无需设置 PYTHONPATH。

同时把 ``tests/`` 放入模块搜索路径，使 ``support`` 能以顶层模块导入。
"""

import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SRC = TESTS.parent / "src"
for path in (str(SRC), str(TESTS)):
    if path not in sys.path:
        sys.path.insert(0, path)
