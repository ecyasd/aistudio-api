// Accept one exact identity. Repeated query parameters never broaden a filter.
export function diagnosticContext(query: Record<string, unknown>) {
  const account = query.account_id
  const request = query.request_id
  return {
    account: typeof account === 'string' && /^(?:acc_[a-f0-9]{8}|[a-f0-9]{32}|[a-f0-9-]{36})$/.test(account) ? account : '',
    request: typeof request === 'string' && /^(?:req_[a-f0-9]{8,32}|[a-f0-9]{32}|[a-f0-9-]{36})$/.test(request) ? request : '',
  }
}
