"""文本 / 表格导入自选。

与截图 OCR 导入平级的数据来源：解析粘贴的剪贴板文本（Excel 复制即 TSV）或上传的
txt / csv / xlsx，产出与 OCR 完全一致的候选结构，前端因此可以共用同一套「候选确认
列表」，后端也复用同一套主数据反查与校验。

三个由真实主数据决定的要点（对本地 instruments.parquet 实测得出）：

1. **前导零必须补回** —— Excel 中以数字格式存放的 ``000697`` 粘贴出来是 ``697``，
   而 A 股有 4356 只代码以 0 开头。
2. **不能假定列位置** —— 用户常整行复制（代码/名称/现价/涨幅…），需要嗅探哪一列是
   代码列、哪一列是名称列；嗅探失败时逐单元格兜底扫描。
3. **名称反查须限定 A 股** —— 主数据是全市场（美股 12426 / A 股 5563 / 港股 2902），
   ``百济神州`` 等名称跨市场重复；限定 ``region=CN`` 后 A 股名称 100% 唯一。
"""
from __future__ import annotations

import logging
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import polars as pl

from app.services.watchlist_ocr.pipeline import (
    ImportCandidate,
    build_instrument_lookups,
    resolve_candidates,
)

logger = logging.getLogger(__name__)

# 单次导入行数上限：防误粘整张行情表（2000 行已远超正常自选规模）
MAX_ROWS = 2000
# 粘贴文本长度上限（约 2000 行 × 40 字）
MAX_TEXT_CHARS = 400_000
# 上传文件体积上限
MAX_FILE_BYTES = 5 * 1024 * 1024

# 列嗅探：命中率低于此值视为该列不是代码/名称列
_COLUMN_HIT_THRESHOLD = 0.5
# 列嗅探采样行数（大表取前若干行即可判定）
_SNIFF_SAMPLE = 200
# 列嗅探最大列数（防异常宽表）
_MAX_COLUMNS = 32

# 首行判定为表头的关键词（命中其一即跳过该行）
_HEADER_HINTS = (
    "代码", "名称", "简称", "股票", "证券", "序号", "现价", "最新价", "涨跌幅",
    "code", "name", "symbol", "ticker", "price",
)

# 名称前缀标记：*ST / ST / N(新股) / C(次新) / U(未盈利) / W(同股不同权) / XD XR DR(除权除息)
# 多字符模式写在单字符之前，否则 'DR' 会被单字符 'D' 抢先匹配。
_NAME_PREFIX_RE = re.compile(r"^(?:\*?ST|XD|XR|DR|N|C|U|W)", re.IGNORECASE)
# '600519' / 'SH600519' / '600519.SH' / '600519.sz'
_CODE_BODY_RE = re.compile(r"^(?:SH|SZ|BJ)?(\d{1,6})(?:\.(?:SH|SZ|BJ))?$", re.IGNORECASE)
# Polars 把整列推断为 Float 时，'600519' 会变成 '600519.0'
_INT_FLOAT_RE = re.compile(r"^(\d+)\.0+$")
_WHITESPACE_RE = re.compile(r"\s+")
# 单元格内嵌的 6 位代码：无分隔符混排时用，如 '600519贵州茅台1680.00'
# 前后界 (?<!\d)/(?!\d) 保证只取完整的 6 位，不会从 '1680.00' 或 '20260915' 里截出代码
_SUBSTR_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")

_SUPPORTED_TEXT_SUFFIXES = (".txt", ".csv")


@dataclass
class DetectedLayout:
    """解析布局元信息，回传给前端做「识别到第 N 列是代码」这类反馈。"""

    delimiter: str = "none"
    code_column: int | None = None
    name_column: int | None = None
    skipped_header: bool = False
    total_rows: int = 0
    used_rows: int = 0
    truncated: bool = False
    unmatched_inputs: list[str] = field(default_factory=list)


def _to_halfwidth(text: str) -> str:
    """全角数字/字母/标点 → 半角（中文输入法与部分行情软件导出常见的坑）。"""
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


def _decode_bytes(raw: bytes) -> str:
    """按 UTF-8 → GB18030 → GBK → Big5 顺序解码。

    国内行情软件（同花顺/东财/通达信）与 Windows 中文 Excel 导出的 CSV 多为 GBK 系编码，
    直接按 UTF-8 解析会失败。与 ``ext_data.ensure_utf8_csv`` 采用同一套编码尝试顺序。
    """
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    for enc in ("gb18030", "gbk", "big5"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _code_core(raw: str) -> str | None:
    """提取单元格里的 A 股代码数字主体（**不补零**）；不像代码则返回 None。

    支持 ``000697`` / ``697`` / `` 600519 `` / ``600519.SH`` / ``SH600519`` /
    ``600519.0``（Polars Float 产物）。返回长度用于区分证据强度：
    原样 6 位是高置信代码，需要补零的则可能是序号列或价格（见 ``_sniff_columns``）。
    """
    s = _to_halfwidth(str(raw)).strip()
    if not s:
        return None
    m = _INT_FLOAT_RE.match(s)
    if m:
        s = m.group(1)
    m = _CODE_BODY_RE.match(s)
    if m:
        digits = m.group(1)
    else:
        # 宽松兜底：纯数字串（容忍内部空白，如复制偶发的 '600 519'）
        compact = _WHITESPACE_RE.sub("", s)
        if not compact.isdigit():
            return None
        digits = compact
    return digits if len(digits) <= 6 else None


def _normalize_code(raw: str) -> str | None:
    """把各种写法的 A 股代码归一为 6 位数字串；不像代码则返回 None。

    关键：Excel 数字格式会吃掉前导零（A 股有 4356 只代码以 0 开头），此处左补零还原。
    """
    core = _code_core(raw)
    return core.zfill(6) if core else None


def _find_embedded_code(cell: str, code_to_symbol: dict[str, str]) -> str | None:
    """从混排单元格里抠出 6 位代码，如 ``600519贵州茅台1680.00``。

    仅作为严格解析失败后的兜底，且要求命中主数据，避免把金额/日期数字误当代码。
    """
    for m in _SUBSTR_CODE_RE.finditer(_to_halfwidth(str(cell))):
        code = m.group(1)
        if code in code_to_symbol:
            return code
    return None


def _normalize_name(raw: str) -> str:
    """名称归一化：全角转半角、去掉所有空白。"""
    return _WHITESPACE_RE.sub("", _to_halfwidth(str(raw)).strip())


def _name_keys(raw: str) -> list[str]:
    """名称的候选 key（原样 / 去交易标记），按优先级排序。"""
    base = _normalize_name(raw)
    if not base:
        return []
    keys = [base]
    stripped = _NAME_PREFIX_RE.sub("", base)
    if stripped != base and len(stripped) >= 2:
        keys.append(stripped)
    return keys


def _lookup_name(raw: str, name_to_symbol: dict[str, str]) -> str | None:
    """名称 → symbol；A 股名称唯一，命中即确定。"""
    for key in _name_keys(raw):
        symbol = name_to_symbol.get(key)
        if symbol:
            return symbol
    return None


def _build_cn_name_index(data_dir: Path) -> dict[str, str]:
    """构建 A 股 ``名称 → symbol`` 索引（仅 ``region == "CN"``）。

    必须限定 A 股：主数据含美股 12426 / A 股 5563 / 港股 2902，``百济神州`` 等名称
    跨市场重复；而 A 股内部名称 100% 唯一（5563 行 / 5563 个名称）。
    """
    path = data_dir / "instruments" / "instruments.parquet"
    if not path.exists():
        return {}
    try:
        df = pl.read_parquet(path)
    except Exception as e:  # noqa: BLE001
        logger.warning("build cn name index failed: %s", e)
        return {}
    if "region" not in df.columns or "name" not in df.columns or "symbol" not in df.columns:
        return {}

    out: dict[str, str] = {}
    for symbol, name in df.filter(pl.col("region") == "CN").select(["symbol", "name"]).iter_rows():
        if not symbol or not name:
            continue
        base = _normalize_name(str(name))
        if not base:
            continue
        sym = str(symbol)
        out.setdefault(base, sym)
        # 名称带 *ST / XD 等标记时额外注册去标记版本，用户按简称写也能命中
        stripped = _NAME_PREFIX_RE.sub("", base)
        if stripped != base and len(stripped) >= 2:
            out.setdefault(stripped, sym)
    return out


def _load_indexes(data_dir: Path) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """加载主数据索引，按 ``(data_dir, instruments mtime)`` 缓存，主数据同步后自动失效。"""
    try:
        mtime = (data_dir / "instruments" / "instruments.parquet").stat().st_mtime
    except OSError:
        mtime = 0.0
    return _indexes_cached(str(data_dir), mtime)


@lru_cache(maxsize=8)
def _indexes_cached(
    data_dir_str: str, _mtime: float
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    data_dir = Path(data_dir_str)
    code_to_symbol, symbol_to_name = build_instrument_lookups(data_dir)
    return code_to_symbol, symbol_to_name, _build_cn_name_index(data_dir)


def _sniff_delimiter(lines: list[str]) -> str:
    """嗅探分隔符。Excel 复制是 TSV，故 tab 优先；其次 csv 常见的逗号/分号/竖线。"""
    sample = lines[:20]
    if not sample:
        return "none"
    joined = "\n".join(sample)
    for name, ch in (("tab", "\t"), ("comma", ","), ("semicolon", ";"), ("pipe", "|")):
        # 至少半数行含该分隔符才认（避免把名称里的单个逗号误当分隔符）
        if joined.count(ch) >= max(1, len(sample) // 2):
            return name
    return "none"


def _rows_from_text(text: str) -> tuple[list[list[str]], str]:
    """文本 → 二维单元格矩阵 + 嗅探到的分隔符。"""
    lines = [ln.strip("\ufeff").rstrip("\r") for ln in text.splitlines()]
    lines = [ln for ln in lines if ln.strip()]
    delimiter = _sniff_delimiter(lines)
    if delimiter == "none":
        # 每行一个字段（换行分隔的纯代码/纯名称列表）
        return [[ln.strip()] for ln in lines], delimiter
    sep = {"tab": "\t", "comma": ",", "semicolon": ";", "pipe": "|"}[delimiter]
    rows = [[cell.strip() for cell in ln.split(sep)] for ln in lines]
    return [row[:_MAX_COLUMNS] for row in rows], delimiter


def _df_to_matrix(df: pl.DataFrame) -> list[list[str]]:
    """DataFrame → 字符串矩阵（None → 空串）。"""
    cols = df.columns[:_MAX_COLUMNS]
    return [
        ["" if v is None else str(v) for v in row]
        for row in df.select(cols).iter_rows()
    ]


def _rows_from_excel(file_bytes: bytes) -> list[list[str]]:
    """解析 .xlsx（只读第一个 sheet）。写入临时文件后交给 Polars + fastexcel。

    用 ``has_header=False``：否则 Polars 会把首行当列名吃掉，而用户的表可能根本没有
    表头（首行就是数据），那样会静默丢一行。表头交由 ``_is_header_row`` 按内容判断。
    """
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        tmp_path = tmp_dir / "upload.xlsx"
        tmp_path.write_bytes(file_bytes)
        df = pl.read_excel(tmp_path, has_header=False)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return _df_to_matrix(df)


def _scan_name_symbol(
    row: list[str], name_col: int | None, name_to_symbol: dict[str, str]
) -> str | None:
    """扫出本行命中的名称对应 symbol（名称列优先，再逐格兜底）。"""
    if name_col is not None and name_col < len(row):
        symbol = _lookup_name(row[name_col], name_to_symbol)
        if symbol:
            return symbol
    for cell in row:
        symbol = _lookup_name(cell, name_to_symbol)
        if symbol:
            return symbol
    return None


def _scan_code(
    row: list[str], code_col: int | None, code_to_symbol: dict[str, str]
) -> tuple[str, int] | None:
    """扫出本行命中的代码，返回 ``(6 位代码, 原始数字长度)``。

    代码列优先；单格解析不出时继续扫描本行其余单元格，最后才用「单元格内嵌 6 位数字」
    兜底，使无分隔符混排（``600519贵州茅台``）也能召回。
    """
    cells: list[str] = []
    if code_col is not None and code_col < len(row):
        cells.append(row[code_col])
    cells.extend(row)
    for cell in cells:
        code = _normalize_code(cell)
        if code and code in code_to_symbol:
            return code, len(_code_core(cell) or "")
    for cell in cells:
        code = _find_embedded_code(cell, code_to_symbol)
        if code:
            return code, 6
    return None


def _extract_row(
    row: list[str],
    code_col: int | None,
    name_col: int | None,
    code_to_symbol: dict[str, str],
    name_to_symbol: dict[str, str],
) -> tuple[str | None, str | None, str]:
    """单行抽取，返回 ``(code, symbol, 未命中时的原始输入)``。

    代码与名称都会算，冲突时仲裁：**原样 6 位的代码高置信，直接用**；需要补零才命中的
    代码属弱证据（序号列 ``1,2,3…`` 补零后恰好都是真实代码 ``000001/000002/…``），若与
    本行名称命中冲突则以名称为准 —— A 股名称 100% 唯一，比补零数字可靠。
    """
    symbol_by_name = _scan_name_symbol(row, name_col, name_to_symbol)
    hit = _scan_code(row, code_col, code_to_symbol)

    if hit:
        code, core_len = hit
        if core_len == 6:
            return code, None, ""
        symbol_by_code = code_to_symbol[code]
        if symbol_by_name and symbol_by_name != symbol_by_code:
            return None, symbol_by_name, ""
        return code, None, ""

    if symbol_by_name:
        return None, symbol_by_name, ""

    # 未命中：取首个非空单元格作为给用户看的原始输入
    for cell in row:
        raw = _normalize_name(cell)
        if raw:
            return None, None, raw
    return None, None, ""


def _is_header_row(
    row: list[str], code_to_symbol: dict[str, str], name_to_symbol: dict[str, str]
) -> bool:
    """首行是表头吗 —— 必须「没有命中任何标的」且「含表头关键词」才算。"""
    code, symbol, _ = _extract_row(row, None, None, code_to_symbol, name_to_symbol)
    if code is not None or symbol is not None:
        return False
    joined = "".join(_normalize_name(cell) for cell in row).lower()
    return any(hint in joined for hint in _HEADER_HINTS)


def _best_column(hits: list[float], total: int) -> int | None:
    if total <= 0:
        return None
    best_i: int | None = None
    best_hit = 0.0
    for i, hit in enumerate(hits):
        if hit > best_hit:
            best_i, best_hit = i, hit
    if best_i is None or best_hit / total < _COLUMN_HIT_THRESHOLD:
        return None
    return best_i


def _sniff_columns(
    rows: list[list[str]],
    code_to_symbol: dict[str, str],
    name_to_symbol: dict[str, str],
) -> tuple[int | None, int | None]:
    """列嗅探：按「该列单元格能匹配上主数据的比例」选代码列与名称列。

    代码命中分强弱：原样 6 位记 1 分，需补零才命中记 0.5 分。后者不可靠 —— 序号列
    ``1,2,3…`` 补零后恰好都是真实代码（``000001`` 平安银行、``000002`` 万科A…）。
    因此先算本行的名称命中，若弱代码命中与本行名称推出的 symbol 冲突则判为噪声不计分。
    """
    if not rows:
        return None, None
    width = min(max(len(r) for r in rows), _MAX_COLUMNS)
    sample = rows[:_SNIFF_SAMPLE]
    code_hits = [0.0] * width
    name_hits = [0.0] * width

    for row in sample:
        row_name_symbol: str | None = None
        for i in range(width):
            cell = row[i] if i < len(row) else ""
            if not cell.strip():
                continue
            symbol = _lookup_name(cell, name_to_symbol)
            if symbol:
                name_hits[i] += 1
                if row_name_symbol is None:
                    row_name_symbol = symbol
        for i in range(width):
            cell = row[i] if i < len(row) else ""
            core = _code_core(cell)
            if core is None:
                continue
            if len(core) == 6:
                if core in code_to_symbol:
                    code_hits[i] += 1
            elif core.zfill(6) in code_to_symbol:
                if row_name_symbol and row_name_symbol != code_to_symbol[core.zfill(6)]:
                    continue
                code_hits[i] += 0.5

    total = len(sample)
    code_col = _best_column(code_hits, total)
    name_col = _best_column(name_hits, total)
    if name_col is not None and name_col == code_col:
        name_col = None
    return code_col, name_col


def _analyze(
    rows: list[list[str]],
    *,
    source: str,
    provider: str,
    delimiter: str,
    data_dir: Path,
    existing_symbols: set[str] | None,
) -> dict[str, Any]:
    """矩阵 → 候选结果（与 OCR 导入完全一致的返回结构）。"""
    layout = DetectedLayout(delimiter=delimiter, total_rows=len(rows))
    if len(rows) > MAX_ROWS:
        layout.truncated = True
        rows = rows[:MAX_ROWS]

    code_to_symbol, symbol_to_name, name_to_symbol = _load_indexes(data_dir)
    if not code_to_symbol:
        raise ValueError("证券主数据为空，请先在「数据」页完成初始化同步")

    if rows and _is_header_row(rows[0], code_to_symbol, name_to_symbol):
        layout.skipped_header = True
        rows = rows[1:]

    layout.code_column, layout.name_column = _sniff_columns(rows, code_to_symbol, name_to_symbol)

    codes: list[str] = []
    unmatched: list[ImportCandidate] = []
    seen_codes: set[str] = set()

    for row in rows:
        code, symbol, raw = _extract_row(
            row, layout.code_column, layout.name_column, code_to_symbol, name_to_symbol
        )
        # 代码命中与名称命中两条路径都归一到 6 位 code，统一交给 resolve_candidates
        # 解析 symbol/名称与「已在自选」标记，保证与 OCR 路径产出完全一致。
        if symbol is None and code:
            symbol = code_to_symbol.get(code)
        if symbol:
            bare = symbol.split(".", 1)[0]
            if len(bare) == 6 and bare.isdigit() and bare not in seen_codes:
                seen_codes.add(bare)
                codes.append(bare)
        elif raw:
            if len(layout.unmatched_inputs) < 50:
                layout.unmatched_inputs.append(raw)
            unmatched.append(ImportCandidate(code=raw, symbol=None, name=None, matched=False))

    matched = resolve_candidates(codes, code_to_symbol, symbol_to_name, existing_symbols)
    candidates = [c.to_dict() for c in matched] + [c.to_dict() for c in unmatched]
    layout.used_rows = len(codes) + len(unmatched)

    unmatched_count = sum(1 for c in candidates if not c["matched"])
    return {
        "source": source,
        "provider": provider,
        "codes": codes,
        "candidates": candidates,
        "matched_count": len(candidates) - unmatched_count,
        "unmatched_count": unmatched_count,
        "detected": {
            "delimiter": layout.delimiter,
            "code_column": layout.code_column,
            "name_column": layout.name_column,
            "skipped_header": layout.skipped_header,
            "total_rows": layout.total_rows,
            "used_rows": layout.used_rows,
            "truncated": layout.truncated,
            "unmatched_inputs": layout.unmatched_inputs,
        },
    }


def import_watchlist_text(
    text: str,
    data_dir: Path,
    *,
    existing_symbols: set[str] | None = None,
) -> dict[str, Any]:
    """解析粘贴文本（Excel 复制即 TSV）并返回候选列表（不写入自选）。"""
    if not text or not text.strip():
        raise ValueError("粘贴内容为空")
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError(f"内容过长（上限约 {MAX_TEXT_CHARS // 1000} 千字符）")
    rows, delimiter = _rows_from_text(text)
    if not rows:
        raise ValueError("未解析到有效内容")
    return _analyze(
        rows,
        source="text",
        provider="clipboard",
        delimiter=delimiter,
        data_dir=data_dir,
        existing_symbols=existing_symbols,
    )


def import_watchlist_file(
    file_bytes: bytes,
    filename: str,
    data_dir: Path,
    *,
    existing_symbols: set[str] | None = None,
) -> dict[str, Any]:
    """解析上传的 txt / csv / xlsx 并返回候选列表（不写入自选）。"""
    if not file_bytes:
        raise ValueError("空文件")
    if len(file_bytes) > MAX_FILE_BYTES:
        raise ValueError(f"文件过大（上限 {MAX_FILE_BYTES // 1024 // 1024}MB）")

    suffix = Path(filename or "").suffix.lower()
    if suffix == ".xls":
        raise ValueError("暂不支持旧版 .xls，请用 Excel 另存为 .xlsx 后再导入")

    if suffix in _SUPPORTED_TEXT_SUFFIXES:
        rows, delimiter = _rows_from_text(_decode_bytes(file_bytes))
        if not rows:
            raise ValueError("未解析到有效内容")
        return _analyze(
            rows,
            source="file",
            provider=suffix.lstrip("."),
            delimiter=delimiter,
            data_dir=data_dir,
            existing_symbols=existing_symbols,
        )

    if suffix == ".xlsx":
        try:
            rows = _rows_from_excel(file_bytes)
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"Excel 解析失败: {e}") from e
        if not rows:
            raise ValueError("表格为空")
        return _analyze(
            rows,
            source="file",
            provider="xlsx",
            delimiter="excel",
            data_dir=data_dir,
            existing_symbols=existing_symbols,
        )

    raise ValueError("仅支持 txt / csv / xlsx 文件")
