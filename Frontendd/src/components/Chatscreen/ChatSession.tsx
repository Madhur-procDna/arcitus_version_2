'use client';
import React, { useState, useRef, useEffect, useCallback } from 'react';
import Image from 'next/image';
import { useRouter } from 'next/navigation';
import MessageLeft, { type AssistantMessageMeta } from './MessageLeft';

let lastQueryResult: Record<string, unknown>[] | null = null;

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
import { buildPriorTurnsForQuery, queryChat, getOrCreateSessionIdForChat, publicReplyForSubQueryError, PHARMA_ASSISTANT_FALLBACK_REPLY } from '@/utils/api/chat';
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

function buildPieChartFromText(content: string): NonNullable<AssistantMessageMeta['chart']> | null {
  const data: Record<string, unknown>[] = [];
  const seen = new Set<string>();
  const addSlice = (rawName: string, rawValue: string) => {
    let name = rawName.replace(/\*/g, '').replace(/\bshare\b/i, '').trim();
    if (/remaining|other/i.test(name)) name = 'Other';
    if (!name || seen.has(name.toLowerCase())) return;
    const value = Number(rawValue);
    if (!Number.isFinite(value) || value <= 0) return;
    seen.add(name.toLowerCase());
    data.push({ name, value });
  };

  const shareRe =
    /(?:^|\n)\s*(?:[-*]\s*)?(?:\*\*)?([A-Za-z][A-Za-z /&-]{1,40}?)(?:\s+share)?(?:\*\*)?\s*[:=-]\s*~?(\d+(?:\.\d+)?)\s*%/gi;
  for (const match of content.matchAll(shareRe)) {
    addSlice(match[1], match[2]);
  }

  for (const line of content.split(/\n+/)) {
    const pctMatches = [...line.matchAll(/(\d+(?:\.\d+)?)\s*%/g)];
    const pct = pctMatches.at(-1)?.[1];
    if (!pct) continue;
    if (/\bcommercial\b/i.test(line)) addSlice('Commercial', pct);
    if (/\bmedicare\b/i.test(line)) addSlice('Medicare', pct);
  }

  const remaining = content.match(/remaining\s+(?:share|percentage|portion).*?(?:approximately\s*)?~?(\d+(?:\.\d+)?)\s*%/i);
  if (remaining) {
    addSlice('Other', remaining[1]);
  } else if (/remaining\s+(?:share|percentage|portion)|other payer/i.test(content)) {
    const used = data.reduce((sum, row) => sum + Number(row.value || 0), 0);
    const other = Math.max(0, Number((100 - used).toFixed(2)));
    if (other > 0.1) {
      addSlice('Other', String(other));
    }
  }

  if (data.length < 2) return null;
  return {
    kind: 'pie',
    data,
    title: 'Payer Mix Share',
    description: 'Commercial, Medicare, and other payer share from the previous answer.',
  };
}

function buildRowsFromAnswerText(content: string): Record<string, unknown>[] {
  const rows: Record<string, unknown>[] = [];
  const seen = new Set<string>();

  for (const rawLine of content.split(/\n+/)) {
    const line = rawLine.replace(/^\s*[-*•]\s*/, '').replace(/\*\*/g, '').trim();
    if (!/\b(prescriptions?|trx|payers?)\b/i.test(line)) continue;

    const match = line.match(
      /^([A-Za-z][A-Za-z0-9 .,&'()/-]{1,80}?)(?:\s+(?:leads|follows|has|with|accounts|represents|shows|generated)|\s*:).*?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)/i,
    );
    if (!match) continue;

    const payer = match[1].trim().replace(/\s+/g, ' ');
    if (!payer || seen.has(payer.toLowerCase())) continue;

    const prescriptions = Number(match[2].replace(/,/g, ''));
    if (!Number.isFinite(prescriptions) || prescriptions <= 0) continue;

    seen.add(payer.toLowerCase());
    rows.push({ payer, prescriptions });
  }

  return rows;
}

function buildPieChartFromRows(rows: Record<string, unknown>[]): NonNullable<AssistantMessageMeta['chart']> | null {
  if (rows.length < 2) return null;
  const data = rows
    .map((row) => ({
      name: String(row.payer ?? row.name ?? ''),
      value: Number(row.prescriptions ?? row.value ?? 0),
    }))
    .filter((row) => row.name.trim() && Number.isFinite(row.value) && row.value > 0);
  if (data.length < 2) return null;
  return {
    kind: 'pie',
    data,
    title: 'Top Payers by Prescriptions',
    description: 'Prescription volume split from the previous answer.',
  };
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

  const handleRegenerate = async (question: string) => {
    if (!question || isLoading) return;
    setIsLoading(true);
    try {
      const sessionId = getOrCreateSessionIdForChat(chatId);
      const prior = getLastCompletedExchangeForFollowUp(messages);
      const priorTurns = buildPriorTurnsForQuery(messages);
      const result = await queryChat({
        question,
        sessionId,
        previousQuestion: prior?.previousQuestion,
        previousSql: prior?.previousSql,
        priorTurns: priorTurns.length > 0 ? priorTurns : undefined,
        forceRefresh: true,
      });
      if (result.ok) {
        lastQueryResult = result.data_table || result.chart?.data || result.result_table?.rows || null;
      }
      const botMessage: Message = {
        role: 'assistant',
        content: result.response,
        meta: result.ok
          ? {
              cacheHit: false,
              durationMs: result.duration_ms,
              sql: result.sql,
              chart: result.chart,
              chart_recommendation: result.chart_recommendation,
              result_table: result.result_table,
              data_table: result.data_table,
              row_count: result.row_count,
            }
          : { failed: true },
      };
      // Replace the last assistant message with the regenerated one
      const updatedMessages = [...messages];
      for (let i = updatedMessages.length - 1; i >= 0; i--) {
        if (updatedMessages[i].role === 'assistant') {
          updatedMessages[i] = botMessage;
          break;
        }
      }
      setMessages(updatedMessages);
      persistThread(updatedMessages);
    } catch {
      // ignore
    } finally {
      setIsLoading(false);
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
      // MINIMAL FIX - EXACTLY AS REQUESTED
      const lowerMsg = text.toLowerCase();

      if (/\b(?:give|show|display|make|create)\b.*\b(?:pie|donut)\b|\b(?:pie|donut)\s+chart\b/.test(lowerMsg)) {
        const previousAssistant = [...messages].reverse().find((m) => m.role === 'assistant');
        const parsedRows = previousAssistant ? buildRowsFromAnswerText(previousAssistant.content) : [];
        const chart = previousAssistant
          ? buildPieChartFromText(previousAssistant.content) ?? buildPieChartFromRows(parsedRows)
          : null;
        if (chart) {
          const botMessage: Message = {
            role: 'assistant',
            content: 'Here is the pie chart for the previous answer.',
            meta: { chart, data_table: chart.data },
          };
          const updatedMessages = [...updatedMessagesWithUser, botMessage];
          setMessages(updatedMessages);
          persistThread(updatedMessages);
          setIsLoading(false);
          return;
        }
      }

      // Check 1: Display intent
      const isTableRequest = [
        "tabular form", "give table", "show table",
        "give me table", "in tabular form", "as table",
        "table please", "table form", "show me table",
        "display table", "in table"
      ].some(k => lowerMsg.includes(k));

      if (isTableRequest) {
        const previousAssistant = [...messages].reverse().find((m) => m.role === 'assistant');
        const parsedRows = previousAssistant ? buildRowsFromAnswerText(previousAssistant.content) : [];
        const tableRows = lastQueryResult && lastQueryResult.length > 0 ? lastQueryResult : parsedRows;
        if (tableRows.length > 0) {
          // Reconstruct the table meta so ResultTablePanel can render it correctly
          const newResultTable = { columns: Object.keys(tableRows[0]), rows: tableRows, total_row_count: tableRows.length, truncated: false, truncated_for_storage: false };
          
          const botMessage: Message = {
            role: 'assistant',
            content: 'Here is the data displayed as a table.',
            meta: {
              durationMs: undefined,
              cacheHit: undefined,
              chart_recommendation: undefined,
              result_table: newResultTable,
              data_table: tableRows as AssistantMessageMeta['data_table'],
              result_display_preview_rows: tableRows.length,
            },
          };
          const updatedMessages = [...updatedMessagesWithUser, botMessage];
          setMessages(updatedMessages);
          persistThread(updatedMessages);
          setIsLoading(false);
          return;
        } else {
          const botMessage: Message = {
            role: 'assistant',
            content: "Please ask a data question first before requesting a table.",
          };
          const updatedMessages = [...updatedMessagesWithUser, botMessage];
          setMessages(updatedMessages);
          persistThread(updatedMessages);
          setIsLoading(false);
          return;
        }
      }

      // Check 2: Context follow-up
      let finalQuestionText = text;
      const isContextFollowUp = [
        "of them", "give me their", "what are their",
        "which of these", "from these", "their cities",
        "their specialties", "their region", "their state",
        "of these hcps", "those hcps"
      ].some(k => lowerMsg.includes(k));

      if (isContextFollowUp && lastQueryResult && lastQueryResult.length > 0) {
        // pass to backend WITH the stored data as context
        // do not block, just enrich the request
        const stringifiedData = JSON.stringify(lastQueryResult.slice(0, 50)); // limit to 50 rows so we don't blow up context
        finalQuestionText = `${text}\n\n[System Context: The user is referring to the following previous data results:\n${stringifiedData}]`;
        // we do not return here! we let it flow down to the normal backend call!
      }

      // Everything else flows normally as before
      const sessionId = getOrCreateSessionIdForChat(chatId);
      const prior = getLastCompletedExchangeForFollowUp(messages);
      const priorTurns = buildPriorTurnsForQuery(messages);
      const result = await queryChat({
        question: finalQuestionText, // send the enriched text!
        sessionId,
        previousQuestion: prior?.previousQuestion,
        previousSql: prior?.previousSql,
        priorTurns: priorTurns.length > 0 ? priorTurns : undefined,
      });

      console.log("API RESPONSE KEYS:", Object.keys(result));
      console.log("API RESPONSE SAMPLE:", JSON.stringify(result).slice(0, 500));
      
      // STEP 3 - SAVE IT (ONE LINE FIX)
      if (result.ok) {
        // queryChat returns data_table when success is true, but if it returned a chart, the rows are in chart.data
        lastQueryResult = result.data_table || result.chart?.data || result.result_table?.rows || null;
        console.log("SAVED ROWS:", lastQueryResult?.length);
      }

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
              ? `### Query ${sr.index}\n\n**${sr.question}**\n\n${publicReplyForSubQueryError(sr.error)}`
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
                clarification_needed: null,
          },
        }));
        updatedMessages = [...updatedMessagesWithUser, ...botMessages];
      } else {
        const botMessage: Message = {
          role: 'assistant',
          content: result.response,
          meta: result.ok
            ? {
                cacheHit: result.cache_hit,
                durationMs: result.duration_ms,
                sql: result.sql,
                chart: result.chart,
                chart_recommendation: result.chart_recommendation,
                result_table: result.result_table,
                data_table: result.data_table,
                result_table_multipart_last_part: result.result_table_multipart_last_part,
                row_count: result.row_count,
                result_display_preview_rows: inferResultTablePreviewRowLimit(text),
                clarification_needed: result.clarification_needed,
              }
            : { failed: true },
        };
        updatedMessages = [...updatedMessagesWithUser, botMessage];
      }
      setMessages(updatedMessages);
      persistThread(updatedMessages);
    } catch {
      const errorMessage: Message = {
        role: 'assistant',
        content: PHARMA_ASSISTANT_FALLBACK_REPLY,
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
                  No data found for this query. Try adjusting your filters.
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
                      onSubmitClarification={(text) => {
                        void handleSubmit(text);
                      }}
                      onRegenerate={pairedUserQuestion ? () => void handleRegenerate(pairedUserQuestion) : undefined}
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
