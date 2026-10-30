const openModals: symbol[] = []
let previousOverflow = ''

export function isTopModal(owner: symbol): boolean {
  return openModals.at(-1) === owner
}

export function setModalOpen(owner: symbol, open: boolean) {
  if (typeof document === 'undefined') return
  const index = openModals.indexOf(owner)
  if (open && index === -1) {
    if (!openModals.length) previousOverflow = document.body.style.overflow
    openModals.push(owner)
    document.body.style.overflow = 'hidden'
  } else if (!open && index !== -1) {
    openModals.splice(index, 1)
    if (!openModals.length) document.body.style.overflow = previousOverflow
  }
}
