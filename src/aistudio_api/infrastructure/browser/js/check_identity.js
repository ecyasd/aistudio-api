(args) => {
    try {
        const expected = (typeof args === 'string' ? args : args?.email || '').trim().toLowerCase();
        const authUser = args?.authUser !== undefined ? String(args.authUser) : '';
        const url = new URL(window.location.href);
        if (url.protocol !== 'https:' || url.hostname !== 'aistudio.google.com') return false;
        const match = url.pathname.match(/^\/u\/([0-9]+)(?:\/|$)/);
        if (url.pathname.startsWith('/u/') && !match) return false;
        const query = url.searchParams.getAll('authuser');
        if (query.length > 1 || (query.length && !/^[0-9]+$/.test(query[0]))) return false;
        const canonical = value => value.replace(/^0+(?=[0-9])/, '');
        if (match && query.length && canonical(match[1]) !== canonical(query[0])) return false;
        const current = canonical(match ? match[1] : (query[0] || '0'));
        if (authUser && (!/^[0-9]+$/.test(authUser) || current !== canonical(authUser))) return false;
        const activeEmail =
(() => {
    try {
        // Only current account controls are identity evidence. Chooser rows,
        // prompts, cookies and account lists can contain other signed-in users.
        const EMAIL_RE = /[\w.+-]+@[\w-]+\.[a-zA-Z0-9.-]+/g;
        const excluded = '[hidden], [aria-hidden="true"], [role="menu"], [role="listbox"], [role="option"], [role="menuitem"]';
        const tiers = [
            'ms-account-switcher button, button.account-switcher-button, ms-account-switcher .account-switcher-text, .account-switcher-button .account-switcher-text',
            'a[href*="SignOutOptions"], #gb a[aria-label], #gb button[aria-label], a[aria-label^="Google Account:"], button[aria-label^="Google Account:"]'
        ];
        for (const selector of tiers) {
            const emails = new Set();
            for (const el of document.querySelectorAll(selector)) {
                if (el.closest(excluded) || !el.getClientRects().length) continue;
                const style = window.getComputedStyle(el);
                if (style.visibility !== 'visible' || style.display === 'none') continue;
                const text = [el.getAttribute('aria-label') || '', el.getAttribute('title') || '',
                    el.getAttribute('data-email') || '', el.innerText || ''].join(' ');
                for (const email of text.match(EMAIL_RE) || []) emails.add(email.trim().toLowerCase());
            }
            // Conflicting current controls are unknown identity, never permission
            // to select whichever email happens to come first.
            if (emails.size) return emails.size === 1 ? [...emails][0] : null;
        }
    } catch (e) {}
    return null;
})();
        return Boolean(activeEmail && (!expected || activeEmail === expected));
    } catch (e) {}
    return false;
}
