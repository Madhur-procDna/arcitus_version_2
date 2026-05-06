export interface User {
  Username: string;
  ProfileUrl?: string;
}

export interface UserIconProps {
  user: User | null;
  size?: 'sm' | 'md' | 'lg' | 'xl';
  className?: string;
}

export interface Chat {
  id: string;
  title: string;
  timestamp: string;
}