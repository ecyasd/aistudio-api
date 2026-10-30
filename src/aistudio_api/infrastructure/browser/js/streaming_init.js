(args) => {
    let pushBinding = typeof window.__aistudio_stream_resume__ === 'function'
        ? window.__aistudio_stream_resume__ : window.__aistudio_stream_push__;
    if (typeof pushBinding !== 'function') {
        // An explicit pre-dispatch receipt permits repairing the native
        // channel without ever repeating an upstream generation request.
        return {started: false, reason: 'binding_unavailable'};
    }
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
    let phase = 'fetch_pending';
    function push(event) {
        if (typeof pushBinding !== 'function') {
            throw new Error('stream binding unavailable');
        }
        pushBinding(JSON.stringify({...event, rid}));
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
            pending = {seq, resolve, reject, timer, event: {...event, seq}};
            try { push(pending.event); } catch (_) { abort('stream binding unavailable'); }
        });
    }
    streams[rid] = {
        abort,
        state() {
            return {phase: pending ? 'awaiting_ack' : phase,
                seq: pending ? pending.seq : sequence,
                bindingReady: typeof pushBinding === 'function'};
        },
        resume(seq) {
            const replacement = window.__aistudio_stream_resume__;
            if (typeof replacement !== 'function' || closed) return false;
            if (pending && pending.seq !== seq) return false;
            pushBinding = replacement;
            if (pending) push(pending.event);
            return true;
        },
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
            phase = 'read_pending';
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
            // Completion can be lost on the same CDP channel as a body chunk.
            // Keep the original stream recoverable until Python confirms it.
            if (!closed) await send({type: 'done'});
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
    return {started: true};
}
