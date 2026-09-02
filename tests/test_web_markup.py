"""Guard actual markup, not only JavaScript running against a stubbed DOM."""

import re
from html.parser import HTMLParser
from pathlib import Path

HTML = Path(__file__).resolve().parents[1] / "web" / "index.html"
ROOT = HTML.parents[1]
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


def test_product_brand_is_moirai_across_user_facing_surfaces():
    source = HTML.read_text(encoding="utf-8")
    assert "<title>Moirai</title>" in source
    assert re.search(r'class="topbar-brand-text[^>]*>Moirai</span>', source)
    assert "NovelEngine Studio" not in source
    assert 'FastAPI(title="Moirai")' in (ROOT / "web_ui.py").read_text(encoding="utf-8")
    assert "🚀 Moirai - 全自动网文小说生成 Agent 系统" in (ROOT / "main.py").read_text(encoding="utf-8")
    assert (ROOT / "README.md").read_text(encoding="utf-8").startswith("# 📖 Moirai -")


class StrictPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack: list[tuple[str, str]] = []
        self.paths: dict[str, list[tuple[str, str]]] = {}
        self.elements: list[tuple[str, dict, list]] = []
        self.duplicate_attributes: list[tuple[str, list[str]]] = []

    def handle_starttag(self, tag, attrs):
        names = [name for name, _ in attrs]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            self.duplicate_attributes.append((tag, duplicates))
        self.elements.append((tag, dict(attrs), self.stack.copy()))
        element_id = dict(attrs).get("id", "")
        if element_id:
            assert element_id not in self.paths, f"Duplicate ID: {element_id}"
            self.paths[element_id] = [*self.stack, (tag, element_id)]
        if tag not in VOID_TAGS:
            self.stack.append((tag, element_id))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        assert self.stack and self.stack[-1][0] == tag, f"Mismatched </{tag}> at {self.getpos()}: {self.stack[-3:]}"
        self.stack.pop()


def test_page_tags_are_balanced_and_ids_unique():
    page = StrictPage()
    page.feed(HTML.read_text(encoding="utf-8"))
    assert not page.stack
    assert not page.duplicate_attributes


def test_landmarks_skip_link_and_clickable_content_have_native_semantics():
    source = HTML.read_text(encoding="utf-8")
    page = StrictPage()
    page.feed(source)
    by_id = {attrs["id"]: (tag, attrs, parents) for tag, attrs, parents in page.elements if "id" in attrs}
    assert by_id["main-content"][0] == "main"
    assert by_id["main-content"][1]["tabindex"] == "-1"
    assert any(tag == "a" and attrs.get("href") == "#main-content" for tag, attrs, _ in page.elements)
    assert by_id["sidebar"][1]["aria-labelledby"] == "library-title"
    assert by_id["chapters-nav-heading"][2][-1] == ("nav", "")
    assert by_id["reader-scroll"][0] == "article"
    assert by_id["reader-scroll"][1]["aria-labelledby"] == "current-chapter-title"
    assert by_id["novel-title"][0] == "button"
    assert ("h1", "novel-title-heading") in by_id["novel-title"][2]
    for control_id in ("compliance-chip", "btn-rail-toggle"):
        assert by_id[control_id][1]["aria-controls"] == "reader-rail"
        assert by_id[control_id][1]["aria-expanded"] == "false"
    non_native_clicks = re.findall(
        r"<(?:div|span|h[1-6]|li|article|section|aside|p)\b[^>]*\bonclick=[^>]*>", source
    )
    assert all(
        ('id="rail-backdrop"' in tag and 'aria-hidden="true"' in tag) or 'role="option"' in tag
        for tag in non_native_clicks
    ), non_native_clicks
    assert not re.search(r"<i\b[^>]*\bonclick=", source)
    assert 'aria-label="添加自定义违禁词"' in source
    assert 'aria-label="删除自定义违禁词：${escapeHtml(w)}"' in source


def test_static_aria_id_references_resolve():
    page = StrictPage()
    page.feed(HTML.read_text(encoding="utf-8"))
    ids = set(page.paths)
    for _, attrs, _ in page.elements:
        for attribute in ("aria-controls", "aria-labelledby", "aria-describedby", "aria-owns"):
            for target in attrs.get(attribute, "").split():
                assert target in ids, (attribute, target, attrs)


def test_every_modal_starts_with_a_level_two_heading():
    page = StrictPage()
    page.feed(HTML.read_text(encoding="utf-8"))
    masks = {
        attrs["id"] for tag, attrs, _ in page.elements
        if tag == "div" and "modal-mask" in attrs.get("class", "").split()
    }
    first_heading = {}
    for tag, _, parents in page.elements:
        if tag not in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            continue
        for _, parent_id in parents:
            if parent_id in masks:
                first_heading.setdefault(parent_id, tag)
                break
    assert first_heading.keys() == masks
    assert set(first_heading.values()) == {"h2"}


def test_tertiary_text_and_primary_buttons_meet_wcag_aa_contrast():
    source = HTML.read_text(encoding="utf-8")
    light = source.split(":root {", 1)[1].split("}", 1)[0]
    dark = source.split("html.dark {", 1)[1].split("}", 1)[0]

    def token(block, name):
        return re.search(rf"{re.escape(name)}:\s*(#[0-9a-fA-F]{{6}})", block).group(1)

    def luminance(color):
        channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    def contrast(first, second):
        values = sorted((luminance(first), luminance(second)))
        return (values[1] + 0.05) / (values[0] + 0.05)

    for block in (light, dark):
        ink = token(block, "--ink-3")
        for background in ("--bg-soft", "--card-2", "--accent-soft"):
            assert contrast(ink, token(block, background)) >= 4.5, (ink, background)
    for endpoint in ("--primary-start", "--primary-end"):
        assert contrast("#ffffff", token(light, endpoint)) >= 4.5, endpoint
    assert "linear-gradient(120deg, var(--primary-start), var(--primary-end))" in source


def test_config_fieldset_and_dialogs_have_independent_boundaries():
    page = StrictPage()
    page.feed(HTML.read_text(encoding="utf-8"))
    for element_id, path in page.paths.items():
        if element_id.startswith("modal-"):
            assert not any(parent_id.startswith("modal-") for _, parent_id in path[:-1]), element_id
    for field in ("cfg-api-key", "cfg-base-url", "cfg-model", "cfg-writer", "cfg-strictness"):
        assert ("fieldset", "config-fields") in page.paths[field]
        assert ("div", "modal-config") in page.paths[field]
    assert ("fieldset", "config-fields") not in page.paths["config-submit"]


def test_all_form_controls_have_explicit_accessible_names():
    page = StrictPage()
    page.feed(HTML.read_text(encoding="utf-8"))
    labels = {attrs["for"] for tag, attrs, _ in page.elements if tag == "label" and "for" in attrs}
    controls = {attrs["id"] for tag, attrs, _ in page.elements if tag in {"input", "select", "textarea"}}
    assert labels <= controls, "Labels must reference actual form controls"
    for tag, attrs, parents in page.elements:
        if tag in {"input", "textarea", "select"}:
            assert attrs.get("aria-label") or attrs["id"] in labels or any(parent == "label" for parent, _ in parents), attrs


def test_tabs_have_consistent_roles_labels_and_initial_selection():
    page = StrictPage()
    page.feed(HTML.read_text(encoding="utf-8"))
    by_id = {attrs["id"]: attrs for _, attrs, _ in page.elements if "id" in attrs}
    tabs = [attrs for _, attrs, _ in page.elements if attrs.get("role") == "tab"]
    assert len(tabs) == 10
    assert by_id["workspace-tabs"]["role"] == "tablist"
    assert sum(tab["aria-selected"] == "true" for tab in tabs) == 1
    for tab in tabs:
        panel = by_id[tab["aria-controls"]]
        assert panel["role"] == "tabpanel"
        assert panel["aria-labelledby"] == tab["id"]
        assert tab["tabindex"] == ("0" if tab["aria-selected"] == "true" else "-1")


def test_static_and_generated_font_icons_are_decorative():
    source = HTML.read_text(encoding="utf-8")
    icons = re.findall(r"<i\s[^>]*>", source)
    assert len(icons) > 100
    assert all('aria-hidden="true"' in icon for icon in icons)


def test_custom_display_components_cannot_override_hidden_utility():
    source = HTML.read_text(encoding="utf-8")
    assert re.search(r"\.btn\.hidden\s*,\s*\.chip\.hidden\s*\{\s*display:\s*none\s*!important", source)


def test_mobile_topbar_hides_desktop_actions_without_losing_names():
    source = HTML.read_text(encoding="utf-8")
    assert re.search(
        r"@media\s*\(max-width:\s*640px\)[^{]*\{.*?"
        r"\.topbar-desktop-action\s*\{\s*display:\s*none\s*!important",
        source,
        re.DOTALL,
    )
    page = StrictPage()
    page.feed(source)
    actions = [
        attrs for tag, attrs, _ in page.elements
        if tag == "button" and "topbar-desktop-action" in attrs.get("class", "").split()
    ]
    assert {button.get("aria-label") for button in actions} == {"模型与 API", "新建作品"}


def test_mobile_reader_stacks_directory_above_full_width_prose():
    source = HTML.read_text(encoding="utf-8")
    assert "@media (max-width: 640px)" in source
    rules = source.split("@media (max-width: 640px)", 1)[1].split("@media (max-width: 360px)", 1)[0]
    assert re.search(r"#tab-chapters\s*\{[^}]*flex-direction:\s*column", rules)
    assert re.search(r"\.chapter-directory\s*\{[^}]*width:\s*100%\s*!important", rules)
    assert re.search(r"#reader-scroll\s*\{[^}]*min-width:\s*0\s*!important[^}]*width:\s*100%", rules)
    page = StrictPage()
    page.feed(source)
    assert "chapter-directory" in next(
        attrs["class"] for _, attrs, _ in page.elements
        if "chapter-directory" in attrs.get("class", "").split()
    ).split()
    assert "workspace-panels" in page.paths


def test_closed_mobile_sidebar_is_hidden_from_keyboard_and_accessibility_tree():
    source = HTML.read_text(encoding="utf-8")
    rules = source.split("@media (max-width: 1024px)", 1)[1].split("#btn-side-toggle", 1)[0]
    assert re.search(r"\.sidebar\s*\{[^}]*visibility:\s*hidden[^}]*pointer-events:\s*none", rules)
    assert re.search(r"\.sidebar\.open\s*\{[^}]*visibility:\s*visible[^}]*pointer-events:\s*auto", rules)
    page = StrictPage()
    page.feed(source)
    toggle = next(attrs for _, attrs, _ in page.elements if attrs.get("id") == "btn-side-toggle")
    assert toggle["aria-controls"] == "sidebar"
    assert toggle["aria-expanded"] == "false"
