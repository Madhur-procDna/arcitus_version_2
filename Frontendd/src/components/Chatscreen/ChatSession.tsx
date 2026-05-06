/* eslint-disable react-hooks/set-state-in-effect */
'use client';
import React, { useState, useRef, useEffect, useCallback } from 'react';
import Image from 'next/image';
import { useRouter } from 'next/navigation';
import MessageLeft, { type AssistantMessageMeta } from './MessageLeft';

/** Last user question + SQL from the prior assistant turn — re-sent to the API for follow-up anchoring. */
function getLastCompletedExchangeForFollowUp(messages: Message[]): {
  previousQuestion: string;
  previousSql: string;
} | null {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m.role !== 'assistant' || !m.meta?.sql?.trim()) continue;
    const sql = m.meta.sql.trim();
    for (let j = i - 1; j >= 0; j--) {
      if (messages[j].role === 'user') {
        return { previousQuestion: messages[j].content.trim(), previousSql: sql };
      }
    }
    return null;
  }
  return null;
}
import MessageRight from './MessageRight';
import { buildPriorTurnsForQuery, queryChat, getOrCreateSessionIdForChat } from '@/utils/api/chat';
import { clearClientChatSession } from '@/utils/clearChatSession';
import { upsertRecentChat, getMessagesForChat, type RecentChatMessage } from '@/utils/recentChats';
import { inferResultTablePreviewRowLimit } from '@/utils/resultTableDisplay';

interface Message {
  role: 'user' | 'assistant';
  content: string;
  meta?: AssistantMessageMeta;
}

function threadToRecent(messages: Message[]): RecentChatMessage[] {
  return messages.map((m) => ({
    role: m.role,
    content: m.content,
    ...(m.meta ? { meta: m.meta } : {}),
  }));
}

interface ChatSessionProps {
  chatId: string;
}

const ChatSession: React.FC<ChatSessionProps> = ({ chatId }) => {
  const [inputValue, setInputValue] = useState('');
  const [messages, setMessages] = useState<Message[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [hasInitialized, setHasInitialized] = useState(false);

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const messagesContainerRef = useRef<HTMLDivElement>(null);
  const router = useRouter();

  const scrollToBottom = (behavior: ScrollBehavior = 'smooth') => {
    messagesEndRef.current?.scrollIntoView({ behavior });
  };

  useEffect(() => {
    if (messages.length > 0) {
      setTimeout(() => {
        scrollToBottom();
      }, 10);
    }
  }, [messages]);

  const persistThread = useCallback((next: Message[]) => {
    const tryWrite = (payload: Message[]) => {
      sessionStorage.setItem('currentChatMessages', JSON.stringify(payload));
      sessionStorage.setItem('activeChatId', chatId);
    };
    try {
      tryWrite(next);
    } catch {
      const slim: Message[] = next.map((m) => {
        if (m.role !== 'assistant' || !m.meta?.result_table?.rows?.length) return m;
        const maxRows = 150;
        const rows = m.meta.result_table.rows;
        if (rows.length <= maxRows) return m;
        return {
          ...m,
          meta: {
            ...m.meta,
            result_table: {
              ...m.meta.result_table,
              rows: rows.slice(0, maxRows),
              truncated_for_storage: true,
            },
          },
        };
      });
      try {
        tryWrite(slim);
      } catch {
        const noTable: Message[] = slim.map((m) =>
          m.role === 'assistant' && m.meta?.result_table
            ? {
                ...m,
                meta: {
                  ...m.meta,
                  result_table: undefined,
                  result_table_multipart_last_part: undefined,
                },
              }
            : m,
        );
        tryWrite(noTable);
      }
    }
  }, [chatId]);

  useEffect(() => {
    if (!hasInitialized && typeof window !== 'undefined') {
      const activeId = sessionStorage.getItem('activeChatId');
      const storedMessages = sessionStorage.getItem('currentChatMessages');
      if (storedMessages && activeId === chatId) {
        try {
          const parsedMessages = JSON.parse(storedMessages) as unknown;
          if (Array.isArray(parsedMessages)) {
            setMessages(parsedMessages as Message[]);
            setHasInitialized(true);
            return;
          }
        } catch (e) {
          console.error('Failed to parse messages:', e);
        }
      }
      const fromDisk = getMessagesForChat(chatId);
      if (fromDisk?.length) {
        const asMessages = fromDisk as Message[];
        setMessages(asMessages);
        persistThread(asMessages);
      }
      setHasInitialized(true);
    }
  }, [hasInitialized, chatId, persistThread]);

  useEffect(() => {
    if (!hasInitialized || messages.length === 0) return;
    upsertRecentChat(chatId, threadToRecent(messages));
  }, [hasInitialized, chatId, messages]);

  const resetTextareaHeight = () => {
    const textarea = document.querySelector('textarea');
    if (textarea) {
      textarea.style.height = 'auto';
      textarea.style.height = '22px';
      textarea.style.overflowY = 'hidden';
    }
  };

  const handleSubmit = async (presetText?: string) => {
    const text = (presetText ?? inputValue).trim();
    if (!text || isLoading) return;

    setInputValue('');
    resetTextareaHeight();

    const userMessage: Message = {
      role: 'user',
      content: text,
    };

    // Add user message immediately
    const updatedMessagesWithUser = [...messages, userMessage];
    setMessages(updatedMessagesWithUser);
    persistThread(updatedMessagesWithUser);

    // Show loading state
    setIsLoading(true);

    try {
      const sessionId = getOrCreateSessionIdForChat(chatId);
      const prior = getLastCompletedExchangeForFollowUp(messages);
      const priorTurns = buildPriorTurnsForQuery(messages);
      const result = await queryChat({
        question: text,
        sessionId,
        previousQuestion: prior?.previousQuestion,
        previousSql: prior?.previousSql,
        priorTurns: priorTurns.length > 0 ? priorTurns : undefined,
      });

      let updatedMessages: Message[];
      if (
        result.ok &&
        result.sub_results &&
        result.sub_results.length > 0
      ) {
        const subs = result.sub_results;
        const botMessages: Message[] = subs.map((sr, idx) => ({
          role: 'assistant' as const,
          content:
            (sr.error
              ? `### Query ${sr.index}\n\n**${sr.question}**\n\n**Error:** ${sr.error}`
              : `### Query ${sr.index}\n\n**${sr.question}**\n\n${sr.response || ''}`) +
            (idx === 0 && result.duration_ms != null
              ? `\n\n_— ${result.cache_hit ? 'Loaded from cache' : 'Fresh query'} · ${result.duration_ms} ms (total)_`
              : ''),
          meta: {
            cacheHit: idx === 0 ? result.cache_hit : undefined,
            durationMs: idx === 0 ? result.duration_ms : undefined,
            sql: sr.sql,
            chart: sr.chart,
            result_table: sr.result_table,
            row_count: sr.row_count,
            result_display_preview_rows: inferResultTablePreviewRowLimit(sr.question),
          },
        }));
        updatedMessages = [...updatedMessagesWithUser, ...botMessages];
      } else {
        const botMessage: Message = {
          role: 'assistant',
          content: result.ok
            ? result.response
            : `**Something went wrong**\n\n${result.response}\n\n_If the API is down: start uvicorn, open \`/health\` on the same port as \`SDA_BACKEND_URL\` in \`Frontendd/.env.local\`, then restart \`npm run dev\` if you changed that file._`,
          meta: result.ok
            ? {
                cacheHit: result.cache_hit,
                durationMs: result.duration_ms,
                sql: result.sql,
                chart: result.chart,
                result_table: result.result_table,
                result_table_multipart_last_part: result.result_table_multipart_last_part,
                row_count: result.row_count,
                result_display_preview_rows: inferResultTablePreviewRowLimit(text),
              }
            : { failed: true },
        };
        updatedMessages = [...updatedMessagesWithUser, botMessage];
      }
      setMessages(updatedMessages);
      persistThread(updatedMessages);
    } catch (error) {
      const detail =
        error instanceof Error ? error.message : 'Unknown error. Check the browser console.';
      const errorMessage: Message = {
        role: 'assistant',
        content: `**Something went wrong**\n\n${detail}\n\n_If the API is down: start uvicorn, open \`/health\` on the same port as \`SDA_BACKEND_URL\` in \`Frontendd/.env.local\`, then restart \`npm run dev\` if you changed that file._`,
        meta: { failed: true },
      };

      const updatedMessages = [...updatedMessagesWithUser, errorMessage];
      setMessages(updatedMessages);
      persistThread(updatedMessages);
    } finally {
      setIsLoading(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  };

  return (
    <div
      className="flex flex-col h-full bg-repeat"
      style={{
        backgroundImage: 'url(/Images/BackgroundImage.png)',
        backgroundSize: 'auto',
        backgroundPosition: '0 0',
      }}
    >
      {/* Messages Container */}
      <div className="flex-1 mx-auto w-full px-8 overflow-hidden mb-[30px] mt-[10px] min-h-0">
        <div
          ref={messagesContainerRef}
          className="h-full overflow-y-auto custom-scrollbar pb-4"
          style={{
            overflowAnchor: 'none',
            scrollbarGutter: 'stable',
          }}
        >
          <div className="py-4">
            {!hasInitialized ? (
              <div className="flex justify-center items-center py-12">
                <div className="text-black">Loading...</div>
              </div>
            ) : messages.length === 0 ? (
              <div className="max-w-4xl mx-auto mt-12 px-2">
                <h2 className="text-xl font-semibold text-gray-800">Start a conversation</h2>
                <p className="text-sm text-black mt-2 mb-6">
                  Ask a question about your data in the box below.
                </p>
                <button
                  type="button"
                  className="text-sm text-primary font-medium underline-offset-2 hover:underline"
                  onClick={() => {
                    clearClientChatSession();
                    router.push('/');
                  }}
                >
                  Back to home
                </button>
              </div>
            ) : (
              <>
                {messages.map((message, index) => {
                  if (message.role === 'user') {
                    return <MessageRight key={index} content={message.content} />;
                  }
                  let pairedUserQuestion: string | undefined;
                  for (let i = index - 1; i >= 0; i -= 1) {
                    if (messages[i].role === 'user') {
                      pairedUserQuestion = messages[i].content;
                      break;
                    }
                  }
                  return (
                    <MessageLeft
                      key={index}
                      content={message.content}
                      isLoading={false}
                      meta={message.meta}
                      pairedUserQuestion={pairedUserQuestion}
                    />
                  );
                })}
                {isLoading && <MessageLeft content="" isLoading={true} />}
                <div ref={messagesEndRef} />
              </>
            )}
          </div>
        </div>
      </div>

      {/* Input Container - Fixed to bottom */}
      <div className="flex-shrink-0 mx-auto w-full px-8 pb-8">
        <div className="bg-white rounded-[12px] shadow-[0px_0px_12px_0px_#0000001A]">
          <div className="pl-[12px] pr-[14px] py-[12px]">
            <div className="flex items-start pl-1 text-sm justify-between gap-2">
              <Image
                src="/Images/Star.svg"
                alt="Star"
                width={20}
                height={20}
                className="flex-shrink-0 "
              />
              <textarea
                value={inputValue}
                onChange={(e) => setInputValue(e.target.value)}
                onKeyDown={handleKeyDown}
                disabled={isLoading}
                placeholder="What would you like to know?"
                rows={1}
                className="flex-1 text-black custom-scrollbar font-normal text-[14px] leading-[22px] bg-transparent border-none outline-none resize-none overflow-hidden min-h-[22px] max-h-[88px]"
                style={{
                  height: 'auto',
                  minHeight: '22px',
                  maxHeight: '88px',
                }}
                onInput={(e) => {
                  const target = e.target as HTMLTextAreaElement;
                  target.style.height = 'auto';
                  target.style.height = `${Math.min(target.scrollHeight, 88)}px`;
                  target.style.overflowY = target.scrollHeight > 88 ? 'scroll' : 'hidden';
                }}
              />
              <button
                type="button"
                onClick={() => void handleSubmit()}
                className="flex mt-0.5 cursor-pointer items-center justify-end rounded-full transition-colors duration-200 flex-shrink-0"
                disabled={!inputValue.trim() || isLoading}
              >
                <Image src="/Images/SendIcon.svg" alt="Submit" width={18} height={22} />
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

export default ChatSession;
