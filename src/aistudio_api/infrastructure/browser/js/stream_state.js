(rid) => {
    const stream = window.__AISTUDIO__?.streams?.[rid];
    return typeof stream?.state === 'function' ? stream.state() : null;
}
