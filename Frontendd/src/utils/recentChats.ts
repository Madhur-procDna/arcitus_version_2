// import type { Chat } from '@/utils/types';

// const STORAGE_KEY = 'compass_recent_chats_v1';
// const MAX_CHATS = 50;
// const MAX_TITLE_LEN = 72;

// export interface RecentChatMessage {
//   role: 'user' | 'assistant';
//   content: string;
//   meta?: {
//     cacheHit?: boolean;
//     durationMs?: number;
//     sql?: string;
//     failed?: boolean;
//     chart?: { kind: 'bar' | 'pie' | 'line'; data: Record<string, unknown>[] };
//     result_table?: {
//       columns: string[];
//       rows: Record<string, unknown>[];
//       total_row_count?: number;
//       truncated?: boolean;
//       truncated_for_storage?: boolean;
//       suppress_remaining_rows?: boolean;
//     };
//     result_table_multipart_last_part?: boolean;
//     row_count?: number;
//     result_display_preview_rows?: number;
//   };
// }

// interface RecentChatRecord {
//   id: string;
//   title: string;
//   updatedAt: number;
//   messages: RecentChatMessage[];
// }

// function readRecords(): RecentChatRecord[] {
//   if (typeof window === 'undefined') return [];
//   try {
//     const raw = localStorage.getItem(STORAGE_KEY);
//     if (!raw) return [];
//     const parsed = JSON.parse(raw) as unknown;
//     if (!Array.isArray(parsed)) return [];
//     return parsed.filter(
//       (r): r is RecentChatRecord =>
//         r != null &&
//         typeof r === 'object' &&
//         typeof (r as RecentChatRecord).id === 'string' &&
//         typeof (r as RecentChatRecord).title === 'string' &&
//         typeof (r as RecentChatRecord).updatedAt === 'number' &&
//         Array.isArray((r as RecentChatRecord).messages)
//     );
//   } catch {
//     return [];
//   }
// }

// function writeRecords(records: RecentChatRecord[]): void {
//   if (typeof window === 'undefined') return;
//   localStorage.setItem(STORAGE_KEY, JSON.stringify(records));
// }

// function deriveTitle(messages: RecentChatMessage[]): string {
//   const firstUser = messages.find((m) => m.role === 'user');
//   const raw = firstUser?.content?.trim() || 'New chat';
//   const singleLine = raw.replace(/\s+/g, ' ');
//   return singleLine.length > MAX_TITLE_LEN ? `${singleLine.slice(0, MAX_TITLE_LEN - 1)}…` : singleLine;
// }

// function formatRelativeTime(ts: number): string {
//   const s = Math.max(0, Math.floor((Date.now() - ts) / 1000));
//   if (s < 60) return 'Just now';
//   const m = Math.floor(s / 60);
//   if (m < 60) return `${m} min ago`;
//   const h = Math.floor(m / 60);
//   if (h < 24) return `${h} hour${h === 1 ? '' : 's'} ago`;
//   const d = Math.floor(h / 24);
//   if (d < 7) return `${d} day${d === 1 ? '' : 's'} ago`;
//   return new Date(ts).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
// }

// export function notifyRecentChatsChanged(): void {
//   if (typeof window === 'undefined') return;
//   window.dispatchEvent(new CustomEvent('recentChatsUpdated'));
// }

// /** Sidebar list: newest first. */
// export function getRecentChatsForSidebar(): Chat[] {
//   const records = readRecords();
//   return [...records]
//     .sort((a, b) => b.updatedAt - a.updatedAt)
//     .map((r) => ({
//       id: r.id,
//       title: r.title,
//       timestamp: formatRelativeTime(r.updatedAt),
//     }));
// }

// export function getMessagesForChat(chatId: string): RecentChatMessage[] | null {
//   const records = readRecords();
//   const found = records.find((r) => r.id === chatId);
//   return found ? found.messages : null;
// }

// /** Persist or update a chat thread (call after each turn). */
// export function upsertRecentChat(chatId: string, messages: RecentChatMessage[]): void {
//   if (typeof window === 'undefined' || !chatId || messages.length === 0) return;

//   const records = readRecords().filter((r) => r.id !== chatId);
//   const title = deriveTitle(messages);
//   const next: RecentChatRecord = {
//     id: chatId,
//     title,
//     updatedAt: Date.now(),
//     messages: messages.map((m) => ({
//       role: m.role,
//       content: m.content,
//       ...(m.meta ? { meta: m.meta } : {}),
//     })),
//   };
//   records.unshift(next);
//   writeRecords(records.slice(0, MAX_CHATS));
//   notifyRecentChatsChanged();
// }

import type { Chat } from '@/utils/types';

const STORAGE_KEY = 'compass_recent_chats_v1';
const MAX_CHATS = 50;
const MAX_TITLE_LEN = 72;

export interface RecentChatMessage {
  role: 'user' | 'assistant';
  content: string;
  meta?: {
    cacheHit?: boolean;
    durationMs?: number;
    sql?: string;
    failed?: boolean;
    chart?: { kind: 'bar' | 'pie' | 'line' | 'stacked_bar'; data: Record<string, unknown>[] };
    result_table?: {
      columns: string[];
      rows: Record<string, unknown>[];
      total_row_count?: number;
      truncated?: boolean;
      truncated_for_storage?: boolean;
      suppress_remaining_rows?: boolean;
    };
    result_table_multipart_last_part?: boolean;
    row_count?: number;
    result_display_preview_rows?: number;
  };
}

interface RecentChatRecord {
  id: string;
  title: string;
  updatedAt: number;
  messages: RecentChatMessage[];
}

function readRecords(): RecentChatRecord[] {
  if (typeof window === 'undefined') return [];
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (r): r is RecentChatRecord =>
        r != null &&
        typeof r === 'object' &&
        typeof (r as RecentChatRecord).id === 'string' &&
        typeof (r as RecentChatRecord).title === 'string' &&
        typeof (r as RecentChatRecord).updatedAt === 'number' &&
        Array.isArray((r as RecentChatRecord).messages)
    );
  } catch {
    return [];
  }
}

/** Strips heavy data from meta before storage to avoid QuotaExceededError. */
function stripMetaForStorage(meta: RecentChatMessage['meta']): RecentChatMessage['meta'] {
  if (!meta) return undefined;
  const { chart, result_table, ...lightMeta } = meta;

  return {
    ...lightMeta,
    // Keep chart kind but drop the potentially huge data array
    ...(chart ? { chart: { kind: chart.kind, data: [] } } : {}),
    // Keep column headers but drop rows; mark as truncated for storage
    ...(result_table
      ? {
          result_table: {
            columns: result_table.columns,
            rows: [],
            total_row_count: result_table.total_row_count,
            truncated: true,
            truncated_for_storage: true,
          },
        }
      : {}),
  };
}

/** Serializes messages, stripping large meta fields before persisting. */
function serializeMessages(messages: RecentChatMessage[]): RecentChatMessage[] {
  return messages.map((m) => ({
    role: m.role,
    content: m.content,
    ...(m.meta ? { meta: stripMetaForStorage(m.meta) } : {}),
  }));
}

function writeRecords(records: RecentChatRecord[]): void {
  if (typeof window === 'undefined') return;
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(records));
  } catch (e) {
    if (!(e instanceof DOMException && e.name === 'QuotaExceededError')) throw e;

    // Fallback 1: drop messages from older records (keep newest 5 intact)
    const trimmed = records.map((r, i) => (i < 5 ? r : { ...r, messages: [] }));
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(trimmed));
    } catch {
      // Fallback 2: store only metadata — no messages at all
      const minimal = records.map(({ id, title, updatedAt }) => ({
        id,
        title,
        updatedAt,
        messages: [],
      }));
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(minimal));
      } catch {
        // Storage is completely full — silently give up
      }
    }
  }
}

function deriveTitle(messages: RecentChatMessage[]): string {
  const latestUser = [...messages].reverse().find((m) => m.role === 'user');
  const raw = latestUser?.content?.trim() || 'New chat';
  const singleLine = raw.replace(/\s+/g, ' ');
  return singleLine.length > MAX_TITLE_LEN ? `${singleLine.slice(0, MAX_TITLE_LEN - 1)}…` : singleLine;
}

function formatRelativeTime(ts: number): string {
  const s = Math.max(0, Math.floor((Date.now() - ts) / 1000));
  if (s < 60) return 'Just now';
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} hour${h === 1 ? '' : 's'} ago`;
  const d = Math.floor(h / 24);
  if (d < 7) return `${d} day${d === 1 ? '' : 's'} ago`;
  return new Date(ts).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

export function notifyRecentChatsChanged(): void {
  if (typeof window === 'undefined') return;
  window.dispatchEvent(new CustomEvent('recentChatsUpdated'));
}

/** Sidebar list: newest first. */
export function getRecentChatsForSidebar(): Chat[] {
  const records = readRecords();
  return [...records]
    .sort((a, b) => b.updatedAt - a.updatedAt)
    .map((r) => ({
      id: r.id,
      title: r.title,
      timestamp: formatRelativeTime(r.updatedAt),
    }));
}

export function getMessagesForChat(chatId: string): RecentChatMessage[] | null {
  const records = readRecords();
  const found = records.find((r) => r.id === chatId);
  return found ? found.messages : null;
}

/** Persist or update a chat thread (call after each turn). */
export function upsertRecentChat(chatId: string, messages: RecentChatMessage[]): void {
  if (typeof window === 'undefined' || !chatId || messages.length === 0) return;

  const records = readRecords().filter((r) => r.id !== chatId);
  const title = deriveTitle(messages);
  const next: RecentChatRecord = {
    id: chatId,
    title,
    updatedAt: Date.now(),
    messages: serializeMessages(messages),
  };
  records.unshift(next);
  writeRecords(records.slice(0, MAX_CHATS));
  notifyRecentChatsChanged();
}