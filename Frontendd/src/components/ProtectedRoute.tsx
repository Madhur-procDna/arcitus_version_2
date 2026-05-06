// components/ProtectedRoute.tsx
'use client';
import { useEffect } from 'react';
import { useRouter, usePathname } from 'next/navigation';
import { useAuth } from '@/context/AuthContext';

export const ProtectedRoute: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { isAuthenticated } = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (!isAuthenticated) {
      // Store the intended destination
      sessionStorage.setItem('redirectAfterLogin', pathname);
      router.push('/login');
    }
  }, [isAuthenticated, router, pathname]);

  // Show loading screen while redirecting
  if (!isAuthenticated) {
    return (
      <div
        className="h-full bg-white flex items-center justify-center bg-repeat"
        style={{
          backgroundImage: 'url(/Images/BackgroundImage.png)',
          backgroundSize: 'auto',
          backgroundPosition: '0 0',
        }}
      >
        <div className="bg-white rounded-2xl shadow-2xl p-12 border border-gray-100 text-center max-w-md">
          <div className="flex flex-col items-center space-y-4">
            {/* Spinner */}
            <svg
              className="animate-spin h-12 w-12 text-primary"
              xmlns="http://www.w3.org/2000/svg"
              fill="none"
              viewBox="0 0 24 24"
            >
              <circle
                className="opacity-25"
                cx="12"
                cy="12"
                r="10"
                stroke="currentColor"
                strokeWidth="4"
              ></circle>
              <path
                className="opacity-75"
                fill="currentColor"
                d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"
              ></path>
            </svg>
            
            {/* Text */}
            <h2
              className="text-2xl font-bold bg-clip-text text-transparent"
              style={{
                background: 'linear-gradient(180deg, #001e96 0%, #001e96 100%)',
                WebkitBackgroundClip: 'text',
                WebkitTextFillColor: 'transparent',
                backgroundClip: 'text',
              }}
            >
              Redirecting to Login
            </h2>
            <p className="text-black text-sm">
              Please wait while we redirect you to the login page...
            </p>
          </div>
        </div>
      </div>
    );
  }

  return <>{children}</>;
};
