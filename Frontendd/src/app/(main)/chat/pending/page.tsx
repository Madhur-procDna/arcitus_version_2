'use client';
import React, { useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import Image from 'next/image';
import MessageLeft from '@/components/Chatscreen/MessageLeft';
import MessageRight from '@/components/Chatscreen/MessageRight';
import { queryChat, getOrCreateSessionIdForChat, PHARMA_ASSISTANT_FALLBACK_REPLY } from '@/utils/api/chat';
import { upsertRecentChat } from '@/utils/recentChats';

export default function PendingPage() {
  const router = useRouter();
  const [userQuestion, setUserQuestion] = useState('');
  // Use a ref instead of state so setting it never triggers an effect re-run.
  // When `started` was state it appeared in the effect dependency array, causing
  // the effect to fire a second time after setStarted(true) — which could race
  // with the in-flight API call and redirect back to "/" prematurely.
  const startedRef = useRef(false);

  useEffect(() => {
    const pendingQuestion = sessionStorage.getItem('pendingQuestion');
    const pendingChatId = sessionStorage.getItem('pendingChatId');

    if (!pendingQuestion || !pendingChatId) {
      router.push('/');
      return;
    }

    setUserQuestion(pendingQuestion);
    if (startedRef.current) return;
    startedRef.current = true;

    const run = async () => {
      try {
        const sessionId = getOrCreateSessionIdForChat(pendingChatId);
        const result = await queryChat({
          question: pendingQuestion,
          sessionId,
        });

        const thread = [
          { role: 'user' as const, content: pendingQuestion },
          {
            role: 'assistant' as const,
            content: result.response,
            meta: result.ok
              ? {
                  cacheHit: result.cache_hit,
                  durationMs: result.duration_ms,
                  sql: result.sql,
                  chart: result.chart,
                }
              : { failed: true },
          },
        ];

        sessionStorage.setItem('currentChatMessages', JSON.stringify(thread));
        sessionStorage.setItem('activeChatId', pendingChatId);
        upsertRecentChat(pendingChatId, thread);
      } catch {
        const thread = [
          { role: 'user' as const, content: pendingQuestion },
          {
            role: 'assistant' as const,
            content: PHARMA_ASSISTANT_FALLBACK_REPLY,
            meta: { failed: true },
          },
        ];
        sessionStorage.setItem('currentChatMessages', JSON.stringify(thread));
        sessionStorage.setItem('activeChatId', pendingChatId);
        upsertRecentChat(pendingChatId, thread);
      } finally {
        sessionStorage.removeItem('pendingQuestion');
        sessionStorage.removeItem('pendingChatId');
        router.replace(`/chat/${pendingChatId}`);
      }
    };

    void run();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [router]);

  return (
    <div
      className="h-full flex flex-col bg-repeat"
      style={{
        backgroundImage: 'url(/Images/BackgroundImage.png)',
        backgroundSize: 'auto',
        backgroundPosition: '0 0',
      }}
    >
      {/* Messages Container */}
      <div className="flex-1 mx-auto w-full px-8 overflow-hidden mb-[30px] mt-[10px]">
        <div className="h-full overflow-y-auto custom-scrollbar pb-4">
          <div className="py-4">
            {userQuestion && <MessageRight content={userQuestion} />}
            <MessageLeft content="" isLoading={true} />
          </div>
        </div>
      </div>

      {/* Footer - Disabled Input - Fixed to bottom */}
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
                disabled={true}
                placeholder="What would you like to know?"
                rows={1}
                className="flex-1 text-black custom-scrollbar font-normal text-[14px] leading-[22px] bg-transparent border-none outline-none resize-none overflow-hidden min-h-[22px] max-h-[88px] opacity-50"
                style={{
                  height: 'auto',
                  minHeight: '22px',
                  maxHeight: '88px',
                }}
              />
              <button
                disabled={true}
                className="flex items-center justify-end rounded-full transition-colors duration-200 flex-shrink-0"
              >
                <div className="flex items-center gap-1 mt-1">
                  <span className="w-1.5 h-1.5 rounded-full bg-[#001e96] animate-bounce" style={{ animationDelay: '0ms' }} />
                  <span className="w-1.5 h-1.5 rounded-full bg-[#001e96] animate-bounce" style={{ animationDelay: '150ms' }} />
                  <span className="w-1.5 h-1.5 rounded-full bg-[#001e96] animate-bounce" style={{ animationDelay: '300ms' }} />
                </div>
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
