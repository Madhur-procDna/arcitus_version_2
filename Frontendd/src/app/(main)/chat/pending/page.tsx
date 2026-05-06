/* eslint-disable react-hooks/set-state-in-effect */
'use client';
import React, { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import Image from 'next/image';
import { BeatLoader } from 'react-spinners';
import MessageLeft from '@/components/Chatscreen/MessageLeft';
import MessageRight from '@/components/Chatscreen/MessageRight';

export default function PendingPage() {
  const router = useRouter();
  const [userQuestion, setUserQuestion] = useState('');

  useEffect(() => {
    const pendingQuestion = sessionStorage.getItem('pendingQuestion');

    if (!pendingQuestion) {
      router.push('/');
      return;
    }

    setUserQuestion(pendingQuestion);
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
                className="flex items-center justify-end rounded-full transition-colors duration-200 flex-shrink-0 opacity-50"
              >
                <BeatLoader size={6} color="#001e96" className='mt-2'/>
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
