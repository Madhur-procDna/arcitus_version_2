// app/(main)/layout.tsx
'use client';
import { useState } from 'react';
import Sidebar from '@/components/Sidebar';
import { ProtectedRoute } from '@/components/ProtectedRoute';

export default function MainLayout({ children }: { children: React.ReactNode }) {
  const [isSidebarCollapsed, setIsSidebarCollapsed] = useState(false);

  return (
    <ProtectedRoute>
      <div className="flex h-full overflow-hidden">
        {/* Sidebar with dynamic width */}
        <div
          className={`flex-shrink-0 transition-all duration-300 ${
            isSidebarCollapsed ? 'w-[4.875rem]' : 'w-[20rem]'
          }`}
        >
          <Sidebar isCollapsed={isSidebarCollapsed} onToggleCollapse={setIsSidebarCollapsed} />
        </div>
        
        {/* Main content area */}
        <main className="flex-1 overflow-y-auto custom-scrollbar bg-gray-50 transition-all duration-300">
          {children}
        </main>
      </div>
    </ProtectedRoute>
  );
}
