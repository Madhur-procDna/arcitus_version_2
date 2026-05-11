// components/ProtectedRoute.tsx
'use client';
import { useEffect } from 'react';
import { useRouter, usePathname } from 'next/navigation';
import { useAuth } from '@/context/AuthContext';

export const ProtectedRoute: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { isAuthenticated, isLoading } = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (isLoading) return;
    if (!isAuthenticated) {
      // Store the intended destination
      sessionStorage.setItem('redirectAfterLogin', pathname);
      router.replace('/login');
      const fallback = window.setTimeout(() => {
        if (window.location.pathname !== '/login') {
          window.location.assign('/login');
        }
      }, 900);
      return () => window.clearTimeout(fallback);
    }
  }, [isAuthenticated, isLoading, router, pathname]);

  if (!isAuthenticated && !isLoading) {
    // Redirect is already handled by the useEffect above.
    // Avoid a synchronous window.location.replace here — it triggers an extra
    // session check (and a visible 401/flash) before the effect-based redirect fires.
    return null;
  }

  return (
    <div className="relative h-full w-full">
      {/* 
        Always render children in this exact wrapper to prevent React from 
        unmounting/remounting the tree when loading state changes. 
      */}
      <div 
        style={{ 
          visibility: isLoading ? 'hidden' : 'visible',
          height: '100%',
          width: '100%',
          ...(isLoading ? { position: 'absolute', inset: 0, pointerEvents: 'none' } : {})
        }}
      >
        {children}
      </div>

      {isLoading && (
        <div className="absolute inset-0 flex items-center justify-center bg-white z-50">
          <div className="flex flex-col items-center gap-3">
            <div className="h-8 w-8 animate-spin rounded-full border-4 border-[#0b5fa5] border-t-transparent" />
            <p className="text-sm text-gray-500">Loading...</p>
          </div>
        </div>
      )}
    </div>
  );
};
