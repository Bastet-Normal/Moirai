// Generated from core/han_repertoire.json.
const nonHanRuns = /[^⺀-⺙⺛-⻳⼀-⿕々〇〡-〩〸-〻㐀-䶿一-鿿豈-舘並-龎𖿢-𖿣𖿰-𖿶𠀀-𪛟𪜀-𫠞𫠠-𬺭𬺰-𮯠𮯰-𮹝丽-𪘀𰀀-𱍊𱍐-𳑹]+/gu;
function countHanCharacters(text) {
    const han = text.replace(nonHanRuns, '');
    // Each supplementary Han character has two UTF-16 units. Count only its leading unit.
    return han.replace(/[\uDC00-\uDFFF]/g, '').length;
}
