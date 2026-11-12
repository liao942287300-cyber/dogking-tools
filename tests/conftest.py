# -*- coding: utf-8 -*-
"""
pytest 配置：修掉 pytest 9 在本目录布局下的一个收集陷阱。

现象
----
`pytest tests/` 会把**所有**用例报成 `ERROR at setup`，而
`python tests/test_pv_store.py` 单独跑却全过。错误信息是：

    ImportError: attempted relative import with no known parent package
    __init__.py:17: from .nodes import NODE_CLASS_MAPPINGS, ...

也就是**插件根目录的 `__init__.py`** 被当成顶层模块 `__init__` 导入了。

原因
----
pytest 9 的 `Module.setup()`（`_pytest/python.py:698`）无条件执行：

    init_mod = importtestmodule(self.path / "__init__.py", self.config)

`self.path` 是测试模块所在**目录**，`tests/` 里没有 `__init__.py`，于是
`import_path()` 拿到一个不存在的路径、退化成 `importlib.import_module("__init__")`。
而 `python -m pytest` 会把当前工作目录（= 插件根目录）放在 `sys.path[0]`，
于是它真的找到了插件的 `__init__.py` —— 那个文件用的是相对导入，必然炸。

修法
----
在收集开始前，往 `sys.modules` 里放一个**无害的空模块**占住 `__init__` 这个名字。
这样 `import_module("__init__")` 直接命中它，不会去碰插件入口文件。

为什么不是「改插件 `__init__.py`」：那是生产代码，测试不该为了跑通去改它。
为什么不是 `--import-mode=importlib`：那是命令行参数，别人直接敲 `pytest tests/`
不会带，得让默认命令就能跑通。
"""

import sys
import types

if "__init__" not in sys.modules:
    _stub = types.ModuleType("__init__")
    # pytest 会读 __file__ 做断言重写/报错定位，给个真实路径免得它拿到 None
    _stub.__file__ = __file__
    sys.modules["__init__"] = _stub
