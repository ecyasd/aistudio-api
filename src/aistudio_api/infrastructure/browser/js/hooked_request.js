(args) => {
    const rid = args.rid;
    window.__AISTUDIO__ = window.__AISTUDIO__ || {};
    const requests = window.__AISTUDIO__.requests = window.__AISTUDIO__.requests || {};
    const controller = new AbortController();
    let reader = null;
    let settled = false;
    let timer;
    return new Promise((resolve) => {
        function finish(result) {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            delete requests[rid];
            resolve(result);
        }
        function abort(reason = 'cancelled') {
            controller.abort();
            if (reader) reader.cancel().catch(() => {});
            finish({status: 0, body: reason});
        }
        requests[rid] = {abort};
        // Keep the deadline active until the entire body has been consumed.
        timer = setTimeout(() => abort('timeout'), (args.timeout || 60) * 1000);
        (async () => {
            try {
                const response = await window.fetch(args.url, {
                    method: 'POST',
                    headers: Object.assign({}, args.headers || {}),
                    body: args.body,
                    credentials: 'include',
                    signal: controller.signal,
                });
                if (settled) return;
                const chunks = [];
                let size = 0;
                let pendingText = '';
                const decoder = new TextDecoder('utf-8', {fatal: true});
                if (response.body) {
                    reader = response.body.getReader();
                    while (!settled) {
                        const {done, value} = await reader.read();
                        if (settled) return;
                        if (done) break;
                        if (!value || !value.byteLength) continue;
                        size += value.byteLength;
                        if (value.byteLength > args.maxReadBytes || size > args.maxResponseBytes) {
                            abort('response size limit exceeded');
                            return;
                        }
                        pendingText += decoder.decode(value, {stream: true});
                        if (pendingText.length >= 65536) {
                            chunks.push(pendingText);
                            pendingText = '';
                        }
                    }
                }
                chunks.push(pendingText + decoder.decode());
                finish({status: response.status, body: chunks.join('')});
            } catch (err) {
                // A failed body read is a failed request, even after HTTP 200.
                abort(err && err.name === 'AbortError' ? 'cancelled' : 'response read failed');
            } finally {
                if (reader) {
                    try { reader.releaseLock(); } catch (_) {}
                }
            }
        })();
    });
}
