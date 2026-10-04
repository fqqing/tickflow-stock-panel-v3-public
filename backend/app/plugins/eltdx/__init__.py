"""通达信(eltdx)行情数据源插件。"""
from .provider import EltdxMinuteProvider, availability

__all__ = ["EltdxMinuteProvider", "availability"]
