(args) => {
    const rid = args.rid;
    window.__AISTUDIO__ = window.__AISTUDIO__ || {};
    const streams = window.__AISTUDIO__.streams = window.__AISTUDIO__.streams || {};
    const aborts = window.__AISTUDIO__.streamAbort = window.__AISTUDIO__.streamAbort || {};
    if (streams[rid]) streams[rid].abort();
    const controller = new AbortController();
    let reader = null;
    let closed = false;
    let timeoutId;
    let pending = null;
    let sequence = 0;
    function push(event) {
        if (typeof window.__aistudio_stream_push__ !== 'function') {
            throw new Error('stream binding unavailable');
        }
        window.__aistudio_stream_push__(JSON.stringify({...event, rid}));
    }
    function cleanup() {
        clearTimeout(timeoutId);
        delete streams[rid];
        delete aborts[rid];
    }
    function abort(message = 'cancelled') {
        if (closed) return;
        closed = true;
        controller.abort();
        if (reader) reader.cancel().catch(() => {});
        if (pending) {
            clearTimeout(pending.timer);
            pending.reject(new Error(message));
            pending = null;
        }
        try { push({type: 'error', message}); } catch (_) {}
        cleanup();
    }
    function resetTimeout() {
        clearTimeout(timeoutId);
        timeoutId = setTimeout(() => abort('timeout'), (args.timeout || 60) * 1000);
    }
    // One unacknowledged event at a time. Python acknowledges only after its
    // downstream consumer resumes, so a paused client also pauses reader.read.
    function send(event) {
        if (closed) return Promise.reject(new Error('cancelled'));
        // Reader progress is deliberately paused until Python consumes this
        // event. Only the consumer deadline applies while waiting for its ACK;
        // upstream silence cannot be measured while no read is in progress.
        clearTimeout(timeoutId);
        return new Promise((resolve, reject) => {
            const seq = ++sequence;
            const timer = setTimeout(() => abort('slow consumer timeout'), args.consumerTimeout * 1000);
            pending = {seq, resolve, reject, timer};
            try { push({...event, seq}); } catch (_) { abort('stream binding unavailable'); }
        });
    }
    streams[rid] = {
        abort,
        ack(seq) {
            if (!pending || pending.seq !== seq) return;
            const current = pending;
            pending = null;
            clearTimeout(current.timer);
            resetTimeout();
            current.resolve();
        },
    };
    aborts[rid] = abort;
    resetTimeout();
    (async () => {
        try {
            const response = await window.fetch(args.url, {
                method: 'POST',
                headers: Object.assign({}, args.headers || {}),
                body: args.body,
                credentials: 'include',
                signal: controller.signal,
            });
            if (closed) return;
            resetTimeout();
            await send({type: 'status', status: response.status || 0});
            const decoder = new TextDecoder('utf-8', {fatal: true});
            let size = 0;
            if (response.body) {
                reader = response.body.getReader();
                while (!closed) {
                    const {done, value} = await reader.read();
                    if (closed) return;
                    if (done) break;
                    if (!value || !value.byteLength) continue;
                    size += value.byteLength;
                    if (value.byteLength > args.maxReadBytes || size > args.maxResponseBytes) {
                        abort('response size limit exceeded');
                        return;
                    }
                    resetTimeout();
                    for (let offset = 0; offset < value.byteLength; offset += args.maxChunkBytes) {
                        const text = decoder.decode(value.subarray(offset, offset + args.maxChunkBytes), {stream: true});
                        if (text) await send({type: 'chunk', text});
                    }
                }
            }
            const remaining = decoder.decode();
            if (remaining) await send({type: 'chunk', text: remaining});
            if (!closed) push({type: 'done'});
            closed = true;
            cleanup();
        } catch (_) {
            abort('stream read failed');
        } finally {
            if (reader) {
                try { reader.releaseLock(); } catch (_) {}
            }
        }
    })();
}
