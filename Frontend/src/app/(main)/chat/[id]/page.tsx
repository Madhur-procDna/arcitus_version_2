'use client';
import { useParams } from 'next/navigation';
import ChatSession from '@/components//Chatscreen/ChatSession';

export default function ChatPage() {
  const params = useParams();
  const chatId = params.id as string;

  return <ChatSession chatId={chatId} />;
}
