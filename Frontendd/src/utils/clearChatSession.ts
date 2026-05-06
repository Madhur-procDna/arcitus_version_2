/**
 * Reset client-side chat state so the next message uses a new API session_id
 * and the UI does not reuse stale sessionStorage threads.
 */
export function clearClientChatSession(): void {
  if (typeof window === 'undefined') return;
  localStorage.removeItem('chatSessionId');
  sessionStorage.removeItem('pendingQuestion');
  sessionStorage.removeItem('pendingChatId');
  sessionStorage.removeItem('currentChatMessages');
  sessionStorage.removeItem('activeChatId');
}
