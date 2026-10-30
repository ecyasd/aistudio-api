export function formatDate(
  date?: string | Date | null,
  opts: { withSeconds?: boolean } = {}
): string {
  if (!date) return '-'
  const d = typeof date === 'string' ? new Date(date) : date
  if (Number.isNaN(d.getTime())) {
    return typeof date === 'string' ? date : '-'
  }
  return d.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    ...(opts.withSeconds ? { second: '2-digit' } : {}),
  })
}

const pacificClock = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/Los_Angeles', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
})

export function millisecondsUntilPacificMidnight(now: Date): number {
  const parts = (date: Date) => Object.fromEntries(
    pacificClock.formatToParts(date).map(part => [part.type, Number(part.value)])
  )
  const current = parts(now)
  const wallMidnight = Date.UTC(current.year!, current.month! - 1, current.day! + 1)
  let target = wallMidnight
  // Resolve the next local midnight with the offset in effect at that time,
  // including the 23/25-hour days at daylight-saving transitions.
  for (let i = 0; i < 3; i++) {
    const local = parts(new Date(target))
    target += wallMidnight - Date.UTC(local.year!, local.month! - 1, local.day!, local.hour!, local.minute!, local.second!)
  }
  return Math.max(0, target - now.getTime())
}
