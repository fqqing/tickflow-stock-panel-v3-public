"""引导 vendor 里的 chan.py 进入 importable 作用域, 并注入本地数据源.

上游 Vespa314/chan.py 要求自定义数据源必须放在 ``DataAPI`` 包下:
``GetStockAPI()`` 里写死了 ``importlib.import_module(f"DataAPI.{name}")``.

直接把我们的文件丢进 vendor 会污染上游代码, 所以这里改用 **regular package
的 __path__ 运行时扩展**: vendor 的 ``DataAPI/__init__.py`` 存在且为空, 属于
常规包, 其 ``__path__`` 可以在运行时追加目录而不会破坏它自身的成员解析.

副作用是把 ``DataAPI`` 变成一个跨两个物理目录的包, 这是刻意的设计:
vendor 保持零改动, 我们自己的 provider 留在 v2 仓库里.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
VENDOR_ROOT = BACKEND_ROOT.parent / "vendor" / "chan_py"
# 注意 __path__ 的元素是「子模块搜索目录」而不是「包根目录」:
# 我们要让 DataAPI.TickflowAPI 可 import, 就得把装着 TickflowAPI.py 的那个
# DataAPI 目录本身追加进去, 而不是它的父目录.
BRIDGE_ROOT = BACKEND_ROOT / "chanlab" / "bridge" / "DataAPI"

_bootstrapped = False


def bootstrap() -> None:
    """幂等地把 chan.py 与本地 bridge 挂进 sys.path / DataAPI.__path__."""
    global _bootstrapped
    if _bootstrapped:
        return

    for path in (str(BACKEND_ROOT), str(VENDOR_ROOT)):
        if path not in sys.path:
            sys.path.insert(0, path)
        if not Path(path).is_dir():
            raise RuntimeError(f"chan.py 依赖目录不存在: {path}")

    # vendor 的 DataAPI 是常规包, __path__ 可以被扩展成多段
    from importlib import invalidate_caches

    import DataAPI

    if str(BRIDGE_ROOT) not in DataAPI.__path__:
        DataAPI.__path__.append(str(BRIDGE_ROOT))
        # __path__ 变了必须让 FileFinder 的目录缓存失效, 否则新目录不参与搜索
        invalidate_caches()

    _bootstrapped = True


def assert_ready() -> None:
    """校验 chan.py 与本地 bridge 均已可见, 供测试与探针前置调用."""
    bootstrap()
    import importlib

    module = importlib.import_module("DataAPI.TickflowAPI")
    if not hasattr(module, "CTickflow"):
        raise RuntimeError("DataAPI.TickflowAPI 中找不到 CTickflow 类")
