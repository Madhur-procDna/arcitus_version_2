'use client';
import React, { useState } from 'react';
import Image from 'next/image';
import { useRouter } from 'next/navigation';

const LandingPage = () => {
  const [inputValue, setInputValue] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const router = useRouter();

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      void handleSubmit();
    }
  };

  const resetTextareaHeight = () => {
    const textarea = document.querySelector('textarea');
    if (textarea) {
      textarea.style.height = 'auto';
      textarea.style.height = '22px';
      textarea.style.overflowY = 'hidden';
    }
  };

  const handleSubmit = async () => {
    if (!inputValue.trim() || isSubmitting) {
      return;
    }
    setIsSubmitting(true);

    const userQuestion = inputValue.trim();
    const chatId = `arcutis-${Date.now()}`;
    sessionStorage.setItem('pendingQuestion', userQuestion);
    sessionStorage.setItem('pendingChatId', chatId);

    resetTextareaHeight();
    setInputValue('');

    // Use only router.push — no hard-reload fallback.
    // The old window.location.assign fallback fired after 250ms while
    // /chat/pending was still compiling (3-4s first load), causing a full
    // page reload that wiped React state and appeared to "lose" the question.
    router.push('/chat/pending');
    setIsSubmitting(false);
  };

  return (
    <div
      className="min-h-full bg-repeat"
      style={{
        backgroundImage: 'url(/Images/BackgroundImage.png)',
        backgroundSize: 'auto',
        backgroundPosition: '0 0',
      }}
    >
      <div className="flex flex-col items-center justify-center gap-y-[20px] px-8 pt-[250px] pb-6 min-h-[60vh]">
        <div className="text-left max-w-4xl w-full fade-in delay-1">
          <h1
            className="text-[50px] font-semibold bg-clip-text text-transparent"
            style={{
              background: 'linear-gradient(180deg, #001e96 0%, #001e96 55%, #c7a33c 100%)',
              WebkitBackgroundClip: 'text',
              WebkitTextFillColor: 'transparent',
              backgroundClip: 'text',
            }}
          >
            Hi User
          </h1>
          <p className="text-black text-[22px] mt-2 font-normal leading-8">
            Have a question? Just ask - your data speaks your language
          </p>

          {/* Input Box */}
          <form
            className="bg-white rounded-[12px] shadow-[0px_0px_12px_0px_#0000001A] max-w-4xl mx-auto mt-[20px] mb-4 fade-in delay-2"
            onSubmit={(e) => {
              e.preventDefault();
              void handleSubmit();
            }}
          >
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
                  placeholder="What would you like to know?"
                  rows={1}
                  disabled={isSubmitting}
                  className="flex-1 text-black custom-scrollbar font-normal text-[16px] leading-[22px] bg-transparent border-none outline-none resize-none overflow-hidden min-h-[22px] max-h-[88px]"
                  style={{
                    height: 'auto',
                    minHeight: '22px',
                    maxHeight: '88px',
                  }}
                  onInput={(e) => {
                    const target = e.target as HTMLTextAreaElement;
                    target.style.height = 'auto';
                    target.style.height = `${Math.min(target.scrollHeight, 88)}px`;
                    if (target.scrollHeight > 88) {
                      target.style.overflowY = 'scroll';
                    } else {
                      target.style.overflowY = 'hidden';
                    }
                  }}
                />

                <button
                  type="submit"
                  className="flex cursor-pointer items-center justify-end rounded-full transition-colors duration-200 flex-shrink-0"
                  disabled={!inputValue.trim() || isSubmitting}
                >
                  <Image src="/Images/SendIcon.svg" alt="Submit" width={22} height={22} />
                </button>
              </div>
            </div>
          </form>
        </div>
      </div>
    </div>
  );
};

export default LandingPage;
