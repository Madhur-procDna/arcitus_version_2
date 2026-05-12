'use client';
import React, { createContext, useContext, useState, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { clearClientChatSession } from '@/utils/clearChatSession';

interface User {
  username: string;
}

interface AuthContextType {
  user: User | null;
  login: (username: string, password: string) => Promise<boolean>;
  logout: () => Promise<void>;
  isAuthenticated: boolean;
  isLoading: boolean;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);
const LOCAL_AUTH_USER_KEY = 'sda_auth_user';

export const AuthProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [user, setUser] = useState<User | null>(null);
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const router = useRouter();

  useEffect(() => {
    let alive = true;
    const hydrateSession = async () => {
      try {
        const resp = await fetch('/api/auth/session', {
          method: 'GET',
          cache: 'no-store',
          signal: AbortSignal.timeout(6000),
        });
        const data = (await resp.json()) as { authenticated?: boolean; username?: string };
        if (!alive) return;
        if (resp.ok && data.authenticated && data.username) {
          setUser({ username: data.username });
          setIsAuthenticated(true);
          sessionStorage.setItem(LOCAL_AUTH_USER_KEY, data.username);
        } else {
          setUser(null);
          setIsAuthenticated(false);
          sessionStorage.removeItem(LOCAL_AUTH_USER_KEY);
        }
      } catch {
        if (!alive) return;
        // Network error — fall back to local storage as best-effort
        const localUser = sessionStorage.getItem(LOCAL_AUTH_USER_KEY);
        if (localUser) {
          setUser({ username: localUser });
          setIsAuthenticated(true);
        } else {
          setUser(null);
          setIsAuthenticated(false);
        }
      } finally {
        if (alive) setIsLoading(false);
      }
    };
    hydrateSession();
    return () => { alive = false; };
  }, []);

  const login = async (username: string, password: string): Promise<boolean> => {
    try {
      const resp = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
        signal: AbortSignal.timeout(8000),
      });
      if (!resp.ok) {
        setUser(null);
        setIsAuthenticated(false);
        return false;
      }
      const data = (await resp.json()) as { username?: string };
      const safeUser = (data.username || username || '').trim() || 'user';
      setUser({ username: safeUser });
      setIsAuthenticated(true);
      sessionStorage.setItem(LOCAL_AUTH_USER_KEY, safeUser);
      const redirectPath = sessionStorage.getItem('redirectAfterLogin');
      if (redirectPath && !redirectPath.startsWith('/login')) {
        sessionStorage.removeItem('redirectAfterLogin');
        router.push(redirectPath);
      } else {
        sessionStorage.removeItem('redirectAfterLogin');
        router.push('/');
      }
      return true;
    } catch {
      setUser(null);
      setIsAuthenticated(false);
      return false;
    }
  };

  const logout = async () => {
    await fetch('/api/auth/logout', { method: 'POST' }).catch(() => undefined);
    setUser(null);
    setIsAuthenticated(false);
    sessionStorage.removeItem(LOCAL_AUTH_USER_KEY);
    clearClientChatSession();
    router.push('/login');
  };

  return (
    <AuthContext.Provider value={{ user, login, logout, isAuthenticated, isLoading }}>
      {children}
    </AuthContext.Provider>
  );
};

export const useAuth = () => {
  const context = useContext(AuthContext);
  if (context === undefined) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
};
