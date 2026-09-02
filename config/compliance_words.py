"""
内容合规词库与扫描器：面向番茄/起点等平台的发布前自查。
词库为可扩展的起步集合（覆盖低俗、血腥、涉政风险、广告法极限词四类），
用户可在此文件中按平台规则自行增删。
"""

from config.settings import get_output_dir
from core.error_reporting import SafeUserMessage
from core.storage import atomic_write_json, read_json

MAX_CUSTOM_COMPLIANCE_BYTES = 128 * 1024
MAX_CUSTOM_CATEGORIES = 50
MAX_CUSTOM_WORDS = 500
MAX_CUSTOM_CATEGORY_CHARS = 12
MAX_CUSTOM_WORD_CHARS = 30


class ComplianceWordsError(SafeUserMessage, RuntimeError):
    """The persisted custom word list is unsafe or cannot be decoded."""

COMPLIANCE_CATEGORIES = {
    "低俗风险": [
        "肉欲", "淫", "呻吟不断", "肌肤大半", "挺动", "花茎", "交合", "春药",
        "全身赤裸", "颤抖不止", "奶子", "屁股蛋", "骚穴",
    ],
    "血腥风险": [
        "肠子", "脑浆", "碎肉", "血肉横飞", "内脏",
    ],
    "涉政风险": [
        "共产党", "国民党", "政府无能", "习近平", "毛主席", "文革", "六四", "法轮功", "台独", "港独",
    ],
    "极限用语": [
        "全网第一", "全网最", "史上最强", "世界第一", "绝对第一", "100%有效", "稳赚不赔",
    ],
}


def _clean_text(value: object, *, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label}必须是文本")
    if "\x00" in value:
        raise ValueError(f"{label}不能包含空字符")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label}包含无效 Unicode 字符") from exc
    clean = value.strip()
    if not clean or len(clean) > maximum:
        raise ValueError(f"{label}需要 1~{maximum} 个字符")
    return clean


def normalize_custom_words(value: object) -> dict[str, list[str]]:
    """Validate and normalize the complete custom dictionary without truncation."""
    if not isinstance(value, dict):
        raise ValueError("自定义词库必须按分类组织")
    if len(value) > MAX_CUSTOM_CATEGORIES:
        raise ValueError(f"自定义词库分类数上限 {MAX_CUSTOM_CATEGORIES}")

    clean: dict[str, list[str]] = {}
    total = 0
    for raw_category, raw_words in value.items():
        category = _clean_text(
            raw_category,
            label="分类名",
            maximum=MAX_CUSTOM_CATEGORY_CHARS,
        )
        if category in clean:
            raise ValueError("去除首尾空白后出现重复分类名")
        if not isinstance(raw_words, list):
            raise ValueError("每个分类的词条必须是列表")
        total += len(raw_words)
        if total > MAX_CUSTOM_WORDS:
            raise ValueError(f"自定义词条总数上限 {MAX_CUSTOM_WORDS}")
        words = [
            _clean_text(word, label="自定义词条", maximum=MAX_CUSTOM_WORD_CHARS)
            for word in raw_words
        ]
        clean[category] = list(dict.fromkeys(words))
    return clean


def get_custom_words() -> dict[str, list[str]]:
    """读取用户自定义合规词库（按分类组织的可持久化扩展词表）"""
    words_file = get_output_dir() / ".custom_compliance.json"
    try:
        return normalize_custom_words(
            read_json(words_file, max_bytes=MAX_CUSTOM_COMPLIANCE_BYTES)
        )
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, ValueError) as exc:
        raise ComplianceWordsError(
            "自定义合规词库不可用，请修复或删除 .custom_compliance.json 后重试"
        ) from exc


def save_custom_words(words: object) -> None:
    words_file = get_output_dir() / ".custom_compliance.json"
    words_file.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(words_file, normalize_custom_words(words))


def scan_text(text: str) -> list:
    """扫描文本，返回命中列表：[{word, category, count, first_pos, context}]，按出现次数降序。"""
    hits = []
    if not text:
        return hits
    merged: dict = {k: list(v) for k, v in COMPLIANCE_CATEGORIES.items()}
    for category, words in get_custom_words().items():
        merged.setdefault(category, [])
        existing = set(merged[category])
        for word in words:
            if word not in existing:
                merged[category].append(word)
                existing.add(word)
    for category, words in merged.items():
        for word in words:
            count = text.count(word)
            if count > 0:
                pos = text.find(word)
                ctx_start = max(0, pos - 10)
                ctx = ("…" if ctx_start > 0 else "") + text[ctx_start:pos + len(word) + 10] + ("…" if pos + len(word) + 10 < len(text) else "")
                hits.append({
                    "word": word,
                    "category": category,
                    "count": count,
                    "first_pos": pos,
                    "context": ctx.replace("\n", " "),
                })
    hits.sort(key=lambda h: h["count"], reverse=True)
    return hits
