(args) => {
    const stream = window.__AISTUDIO__?.streams?.[args.rid];
    if (stream) stream.ack(args.seq);
}
