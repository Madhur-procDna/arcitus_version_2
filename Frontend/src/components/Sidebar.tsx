'use client';
import React, { useState, useEffect, useCallback } from 'react';
import Image from 'next/image';
import { useRouter, usePathname } from 'next/navigation';
import { Chat } from '@/utils/types';
import { clearClientChatSession } from '@/utils/clearChatSession';
import { getRecentChatsForSidebar, getMessagesForChat } from '@/utils/recentChats';

interface SidebarProps {
  isCollapsed?: boolean;
  onToggleCollapse?: (collapsed: boolean) => void;
}

const Sidebar: React.FC<SidebarProps> = ({ isCollapsed = false, onToggleCollapse }) => {
  const [search, setSearch] = useState('');
  // Initialize empty to avoid SSR/client hydration mismatch (localStorage only exists in browser)
  const [chats, setChats] = useState<Chat[]>([]);
  const router = useRouter();
  const pathname = usePathname();

  const refreshChats = useCallback(() => {
    setChats(getRecentChatsForSidebar());
  }, []);

  useEffect(() => {
    // Load chats on mount (client-only)
    refreshChats();
    const onUpdate = () => refreshChats();
    window.addEventListener('recentChatsUpdated', onUpdate);
    window.addEventListener('storage', onUpdate);
    return () => {
      window.removeEventListener('recentChatsUpdated', onUpdate);
      window.removeEventListener('storage', onUpdate);
    };
  }, [refreshChats, pathname]);

  const toggleSidebar = () => {
    onToggleCollapse?.(!isCollapsed);
  };

  const handleNewChat = (e: React.MouseEvent<HTMLButtonElement>) => {
    e.stopPropagation();
    clearClientChatSession();
    router.push('/');
  };

  const handleChatClick = (e: React.MouseEvent, chatId: string) => {
    e.stopPropagation();
    const thread = getMessagesForChat(chatId);
    if (!thread?.length) {
      refreshChats();
      return;
    }
    sessionStorage.setItem('activeChatId', chatId);
    sessionStorage.setItem('currentChatMessages', JSON.stringify(thread));
    router.push(`/chat/${chatId}`);
  };

  const filteredChats = chats.filter((chat) =>
    chat.title.toLowerCase().includes(search.toLowerCase())
  );

  return (
    <div
      className="h-full border-r cursor-pointer bg-white border-gray-200 flex flex-col transition-all duration-300 relative w-full"
      onClick={() => {
        if (isCollapsed) onToggleCollapse?.(false);
      }}
    >
      <div className="p-6 flex flex-col h-full">
        <div className="flex flex-col gap-5">
          {/* Search bar and toggle button */}
          <div className="relative flex items-center">
            {!isCollapsed && (
              <div className="ml-2 relative flex-1">
                <input
                  type="text"
                  placeholder="Search"
                  value={search}
                  className="w-full pr-9 py-2.5 pl-4 bg-secondary text-content rounded-lg text-sm focus:outline-none focus:ring-2"
                  onChange={(e) => setSearch(e.target.value)}
                />
                <Image
                  src="/Images/Search.svg"
                  alt="Search Icon"
                  width={16}
                  height={16}
                  className="absolute right-2 top-1/2 -translate-y-1/2"
                />
              </div>
            )}
            <button
              onClick={(e) => {
                e.stopPropagation();
                toggleSidebar();
              }}
              className="ml-2 bg-white cursor-pointer rounded-full p-1.5 hover:shadow-sm transition-shadow"
              title={isCollapsed ? 'Expand' : 'Collapse'}
            >
              <Image src="/Images/Sidebar.svg" alt="Toggle Sidebar" width={16} height={16} />
            </button>
          </div>

          {/* New Chat button */}
          {!isCollapsed && (
            <button
              onClick={handleNewChat}
              className="mx-2 bg-primary cursor-pointer text-white rounded-xl py-2.5 text-sm font-medium hover:bg-primary-dark transition-opacity"
            >
              New Chat
            </button>
          )}
        </div>

        {/* Recent Chats */}
        {!isCollapsed && (
          <div className="flex-1 my-6 overflow-hidden flex flex-col">
            <h3 className="text-sm font-semibold text-black mb-3 mx-2">Recent Chats</h3>

            {!isCollapsed && <div className="my-1 mx-2 border-t border-gray-200"></div>}

            <div className="flex-1 overflow-y-auto custom-scrollbar">
              {filteredChats.length === 0 ? (
                <p className="text-xs text-black text-center py-4 px-2">
                  {search.trim()
                    ? 'No chats match your search.'
                    : 'No conversations yet. Ask a question from the home page to start.'}
                </p>
              ) : (
                <div className="space-y-1">
                  {filteredChats.map((chat) => {
                    const isActive = pathname === `/chat/${chat.id}`;

                    return (
                      <button
                        key={chat.id}
                        type="button"
                        onClick={(e) => handleChatClick(e, chat.id)}
                        className={`w-full text-left py-3 rounded-lg transition-colors cursor-pointer group ${
                          isActive
                            ? 'bg-[#e8edff] border border-[#cdd8ff]'
                            : 'hover:bg-gray-50 border border-transparent'
                        }`}
                      >
                        <div className="flex items-start gap-2">
                          <Image
                            src="/Images/ChatIcon.svg"
                            alt=""
                            width={16}
                            height={16}
                            className="flex-shrink-0 mt-0.5"
                            onError={(e) => {
                              e.currentTarget.style.display = 'none';
                            }}
                          />
                          <div className="flex-1 min-w-0">
                            <p
                              className={`text-sm font-medium truncate ${
                                isActive ? 'text-[#001e96]' : 'text-gray-800 group-hover:text-primary'
                              }`}
                            >
                              {chat.title}
                            </p>
                            <p className="text-xs text-black mt-0.5">{chat.timestamp}</p>
                          </div>
                        </div>
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

export default Sidebar;
