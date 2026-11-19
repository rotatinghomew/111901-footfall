"""测试启动垫片：把 ``src/`` 放入导入路径。

每个测试模块在导入 ``footfall`` 之前 ``import _bootstrap``，这样
``python -m unittest discover -s tests`` 无需额外环境变量。
"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
