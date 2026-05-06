'use client';
import React from 'react';
import UserIcon from '@/components/UserIcon';
import { MOCK_USER } from '@/utils/constants';

interface MessageRightProps {
  content: string;
}

const MessageRight: React.FC<MessageRightProps> = ({ content }) => {
  return (
    <div className="flex justify-end mb-6 mr-2">
      <div className="flex items-start gap-3 max-w-[70%]">
        {/* Message bubble */}
        <div className="flex-1 bg-[#001e96] text-white rounded-bl-[12px] rounded-tl-[12px] rounded-br-[12px] p-[16px] shadow-sm border border-[#001e96]">
          <p className="text-sm leading-relaxed font-[400] whitespace-pre-wrap">{content}</p>
        </div>
        
        {/* User Icon */}
        <UserIcon user={MOCK_USER} size="md" className="flex-shrink-0" />
      </div>
    </div>
  );
};

export default MessageRight;
