"""
内容合规词库与扫描器：面向番茄/起点等平台的发布前自查。
词库为可扩展的起步集合（覆盖低俗、血腥、涉政风险、广告法极限词四类），
用户可在此文件中按平台规则自行增删。
"""

import json

from config.settings import get_output_dir
from core.storage import atomic_write_json

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


def get_custom_words() -> dict:
    """读取用户自定义合规词库（按分类组织的可持久化扩展词表）"""
    try:
        words_file = get_output_dir() / ".custom_compliance.json"
        if words_file.exists():
            data = json.loads(words_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {k: [str(w) for w in v] for k, v in data.items() if isinstance(v, list)}
    except Exception:
        pass
    return {}


def save_custom_words(words: dict) -> None:
    words_file = get_output_dir() / ".custom_compliance.json"
    words_file.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(words_file, words)


def scan_text(text: str) -> list:
    """扫描文本，返回命中列表：[{word, category, count, first_pos, context}]，按出现次数降序。"""
    hits = []
    if not text:
        return hits
    merged: dict = {k: list(v) for k, v in COMPLIANCE_CATEGORIES.items()}
    for category, words in get_custom_words().items():
        merged.setdefault(category, [])
        existing = set(merged[category])
        merged[category].extend(w for w in words if w and w not in existing)
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
