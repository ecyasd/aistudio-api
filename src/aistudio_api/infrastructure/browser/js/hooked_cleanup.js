(rid) => {
    const request = window.__AISTUDIO__?.requests?.[rid];
    if (request) request.abort();
}
