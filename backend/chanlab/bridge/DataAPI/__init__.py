"""空的包标记.

注意: 这个 __init__.py **不会被加载** -- ``DataAPI`` 包已经由 vendor 里的同名
常规包接管了, 本目录只是被追加进 ``DataAPI.__path__`` 的一节. 留这个文件是为了
目录本身不被当成 namespace package 碎片.
"""
