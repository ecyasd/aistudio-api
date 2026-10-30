(args) => {
    const stream = window.__AISTUDIO__?.streams?.[args.rid];
    return typeof stream?.resume === 'function' ? stream.resume(args.seq) : false;
}
