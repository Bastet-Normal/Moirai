window.safariIssues = [];
for (const level of ['warn', 'error']) {
    const original = console[level].bind(console);
    console[level] = (...args) => { window.safariIssues.push(level + ': ' + args.join(' ')); original(...args); };
}
addEventListener('error', event => window.safariIssues.push(event.message || event.target?.src || 'resource error'), true);
addEventListener('unhandledrejection', event => window.safariIssues.push(String(event.reason)));
addEventListener('securitypolicyviolation', event => window.safariIssues.push('CSP: ' + event.blockedURI));
new PerformanceObserver(list => {
    for (const entry of list.getEntries()) {
        const url = new URL(entry.name, location.href);
        if (['http:', 'https:'].includes(url.protocol) && url.hostname !== '127.0.0.1') window.safariIssues.push('External request: ' + url.origin);
    }
}).observe({ type: 'resource', buffered: true });
