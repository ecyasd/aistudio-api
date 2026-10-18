// Execute production browser scripts with deterministic fetch/read/timer doubles.
// No network requests, profiles, credentials, or model calls.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const scripts = new URL('../../src/aistudio_api/infrastructure/browser/js/', import.meta.url);
const tick = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
const defaults = {rid: 'one', url: 'https://synthetic.invalid', headers: {}, body: '[]', timeout: 120,
    maxResponseBytes: 1024, maxReadBytes: 256, maxChunkBytes: 4, consumerTimeout: 30};
const checks = [];
function browser(fetch) {
    const timers = new Map();
    const events = [];
    let next = 0;
    const window = {fetch, __aistudio_stream_push__: payload => events.push(JSON.parse(payload))};
    const context = vm.createContext({window, AbortController, TextDecoder,
        setTimeout(fn, delay) { const id = ++next; timers.set(id, {fn, delay}); return id; },
        clearTimeout(id) { timers.delete(id); }});
    return {window, timers, events,
        run(name, args) { return vm.runInContext(`(${fs.readFileSync(new URL(name, scripts), 'utf8')})`, context)(args); },
        fire(delay) {
            const entry = [...timers.entries()].find(([, timer]) => timer.delay === delay);
            assert.ok(entry, `timer ${delay} exists`);
            timers.delete(entry[0]); entry[1].fn();
        }};
}
for (const mode of ['stalled body timeout', 'body read rejection', 'explicit cancellation']) {
    let signal;
    let rejectRead;
    let cancelled = false;
    const pending = new Promise((_, reject) => { rejectRead = reject; });
    const reader = {read: () => pending, cancel: async () => { cancelled = true; rejectRead(new Error('cancel')); }, releaseLock() {}};
    const b = browser(async (_, options) => { signal = options.signal; return {status: 200, body: {getReader: () => reader}}; });
    const result = b.run('hooked_request.js', defaults);
    await tick();
    assert.equal(b.timers.size, 1, 'deadline still covers body after headers');
    if (mode === 'stalled body timeout') b.fire(120000);
    else if (mode === 'body read rejection') rejectRead(new Error('synthetic private error'));
    else b.run('hooked_cleanup.js', 'one');
    const output = await result;
    assert.equal(output.status, 0);
    assert.equal(signal.aborted, true);
    assert.equal(cancelled, true);
    assert.equal(output.body.includes('private'), false);
    assert.equal(Object.keys(b.window.__AISTUDIO__.requests).length, 0);
    assert.equal(b.timers.size, 0);
    checks.push(mode);
}
for (const mode of ['response total bound', 'single read bound']) {
    let signal;
    let cancelled = false;
    let reads = 0;
    const reader = {read: async () => ({done: false, value: new Uint8Array(8)}),
        cancel: async () => { cancelled = true; }, releaseLock() {}};
    const b = browser(async (_, options) => { signal = options.signal; return {status: 200, body: {getReader: () => ({...reader, read() { reads++; return reader.read(); }})}}; });
    const output = await b.run('hooked_request.js', {...defaults,
        maxResponseBytes: mode === 'response total bound' ? 12 : 100,
        maxReadBytes: mode === 'single read bound' ? 4 : 16});
    assert.equal(output.status, 0);
    assert.equal(output.body, 'response size limit exceeded');
    assert.equal(signal.aborted, true);
    assert.equal(cancelled, true);
    assert.equal(reads, mode === 'response total bound' ? 2 : 1);
    checks.push(mode);
}
{
    const bytes = new TextEncoder().encode('你好a世界');
    let offset = 0;
    const reader = {read: async () => offset < bytes.length
        ? {done: false, value: bytes.subarray(offset, ++offset)} : {done: true},
        cancel: async () => {}, releaseLock() {}};
    const b = browser(async () => ({status: 200, body: {getReader: () => reader}}));
    const output = await b.run('hooked_request.js', defaults);
    assert.equal(output.status, 200);
    assert.equal(output.body, '你好a世界');
    assert.equal(b.timers.size, 0);
    checks.push('complete nonstream body with one-byte UTF-8 reads');
}
{
    const bytes = new TextEncoder().encode('你好a世界');
    let reads = 0;
    const reader = {read: async () => ++reads === 1 ? {done: false, value: bytes} : {done: true}, cancel: async () => {}, releaseLock() {}};
    const b = browser(async () => ({status: 200, body: {getReader: () => reader}}));
    b.run('streaming_init.js', defaults);
    await tick();
    assert.equal(reads, 0, 'status must be consumed before first body read');
    assert.deepEqual(b.events.map(e => e.type), ['status']);
    b.run('stream_ack.js', {rid: 'one', seq: 99});
    await tick();
    assert.equal(reads, 0, 'wrong acknowledgement cannot release pending event');
    b.run('stream_ack.js', {rid: 'one', seq: 1});
    await tick();
    assert.equal(reads, 1);
    assert.equal(b.events.length, 2, 'only one unacknowledged chunk is emitted');
    for (let i = 0; i < 10 && !b.events.some(e => e.type === 'done'); i++) {
        const last = b.events.at(-1);
        b.run('stream_ack.js', {rid: 'one', seq: last.seq});
        await tick();
    }
    assert.equal(b.events.filter(e => e.type === 'chunk').map(e => e.text).join(''), '你好a世界');
    assert.equal(b.events.at(-1).type, 'done');
    assert.equal(reads, 2);
    assert.equal(b.timers.size, 0);
    assert.equal(Object.keys(b.window.__AISTUDIO__.streams).length, 0);
    checks.push('bounded stream ACK ordering and split UTF-8');
}
{
    let signal;
    const b = browser(async (_, options) => { signal = options.signal; return {status: 200, body: null}; });
    b.run('streaming_init.js', defaults);
    await tick();
    b.fire(30000);
    await tick();
    assert.equal(signal.aborted, true);
    assert.equal(b.events.at(-1).type, 'error');
    assert.equal(b.events.at(-1).message, 'slow consumer timeout');
    assert.equal(Object.keys(b.window.__AISTUDIO__.streams).length, 0);
    assert.equal(b.timers.size, 0);
    checks.push('paused consumer self-cleans browser state');
}
for (const mode of ['stream total bound', 'stream read failure', 'stream cancellation']) {
    let signal;
    let reads = 0;
    let cancelled = false;
    const reader = {read: async () => {
        reads++;
        if (mode === 'stream read failure') throw new Error('private detail');
        return {done: false, value: new TextEncoder().encode('abcd')};
    }, cancel: async () => { cancelled = true; }, releaseLock() {}};
    const b = browser(async (_, options) => { signal = options.signal; return {status: 200, body: {getReader: () => reader}}; });
    b.run('streaming_init.js', {...defaults, maxResponseBytes: 3});
    await tick();
    if (mode === 'stream cancellation') b.run('stream_cleanup.js', 'one');
    else b.run('stream_ack.js', {rid: 'one', seq: 1});
    await tick();
    assert.equal(signal.aborted, true);
    assert.equal(b.events.at(-1).type, 'error');
    assert.equal(b.events.some(e => e.type === 'done'), false);
    assert.equal(b.events.at(-1).message.includes('private'), false);
    if (reads) assert.equal(cancelled, true);
    assert.equal(b.timers.size, 0);
    assert.equal(Object.keys(b.window.__AISTUDIO__.streams).length, 0);
    checks.push(mode);
}
console.log(JSON.stringify({offline: true, passed: checks.length, checks}, null, 2));
