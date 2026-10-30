(expected) => {
    const input = document.querySelector('textarea');
    if (!input || input.value !== expected) return false;
    const button = Array.from(document.querySelectorAll('button')).find(b => {
        const text = (b.innerText || b.textContent || '').trim();
        return text === 'Run' || text.startsWith('Run\n') || text.startsWith('Run Ctrl')
            || text === 'Build' || text.startsWith('Build\n') || text.startsWith('BuildCtrl')
            || b.classList.contains('build-button') || b.classList.contains('ctrl-enter-submits');
    });
    return !!button && !button.disabled && button.getAttribute('aria-disabled') !== 'true';
}
