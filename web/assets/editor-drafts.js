// Browser-only drafts for the local writing desk. The project API owns the saved manuscript.
const editorDrafts = (() => {
    const reviewKey = 'moirai_review_drafts';
    const reviewLimit = 100;

    function raw(store, key) {
        try { return globalThis[store].getItem(key); }
        catch { return null; }
    }

    function parseChapter(value) {
        try {
            const draft = JSON.parse(value);
            return typeof draft?.text === 'string' ? draft : null;
        } catch { return null; }
    }

    function chapterKey(novelId, chapter) {
        return `moirai_editor:${novelId}:${chapter}`;
    }

    function open(key) {
        const local = raw('localStorage', key);
        const tab = raw('sessionStorage', key);
        const tabDraft = parseChapter(tab);
        return {draft:tabDraft || parseChapter(local), ownedValue:tabDraft && tab !== local ? null : local};
    }

    function list(novelId) {
        const prefix = chapterKey(novelId, '');
        const keys = new Set();
        for (const store of ['sessionStorage', 'localStorage']) {
            try {
                const storage = globalThis[store];
                for (let i = 0; i < storage.length; i++) {
                    const key = storage.key(i);
                    if (key?.startsWith(prefix)) keys.add(key.slice(prefix.length));
                }
            } catch { /* One store can remain available while the other is blocked. */ }
        }
        return [...keys].filter(suffix => /^(?:new|\d+(?::new)?)$/.test(suffix) && open(prefix + suffix).draft);
    }

    function newChapterKey(novelId, number) {
        const key = chapterKey(novelId, 'new');
        if (open(key).draft) return key;
        const previous = chapterKey(novelId, number + ':new');
        if (open(previous).draft) return previous;
        const legacy = list(novelId).find(suffix => suffix.endsWith(':new'));
        return legacy ? chapterKey(novelId, legacy) : key;
    }

    function remove(store, key, expected) {
        if (!expected || raw(store, key) !== expected) return;
        try { globalThis[store].removeItem(key); } catch { /* Storage may be blocked. */ }
    }

    function clear(key, ownedValue, tabValue) {
        remove('localStorage', key, ownedValue);
        remove('sessionStorage', key, tabValue);
    }

    function afterSave(key, local, tab, title, text) {
        const matches = value => {
            const draft = parseChapter(value);
            return draft?.title === title && draft?.text === text;
        };
        clear(key, matches(local) ? local : null, matches(tab) ? tab : null);
    }

    function write(key, ownedValue, draft) {
        const value = JSON.stringify(draft);
        if (raw('localStorage', key) === ownedValue) {
            try {
                localStorage.setItem(key, value);
                remove('sessionStorage', key, raw('sessionStorage', key));
                return {store:'local', ownedValue:value};
            } catch { /* Try tab storage when persistence is unavailable or full. */ }
        }
        try {
            sessionStorage.setItem(key, value);
            return {store:'tab', ownedValue};
        } catch { return {store:'unavailable', ownedValue}; }
    }

    function readReviews() {
        try {
            const entries = JSON.parse(raw('sessionStorage', reviewKey) || '[]');
            if (!Array.isArray(entries)) return new Map();
            return new Map(entries.slice(-reviewLimit).filter(entry => Array.isArray(entry) &&
                typeof entry[0] === 'string' && typeof entry[1]?.note === 'string'));
        } catch { return new Map(); }
    }

    function storeReviews(reviews) {
        while (reviews.size > reviewLimit) reviews.delete(reviews.keys().next().value);
        try {
            if (reviews.size) sessionStorage.setItem(reviewKey, JSON.stringify([...reviews]));
            else sessionStorage.removeItem(reviewKey);
        } catch { /* Keep the live tab's in-memory notes available. */ }
    }

    return {chapterKey, newChapterKey, open, list, write, clear, afterSave,
        tabValue:key => raw('sessionStorage', key), readReviews, storeReviews};
})();
