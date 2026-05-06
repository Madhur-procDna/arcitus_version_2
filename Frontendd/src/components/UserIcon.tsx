'use client';
import React from 'react';
import Image from 'next/image';
import type { UserIconProps } from '@/utils/types';

const UserIcon: React.FC<UserIconProps> = ({ user, size = 'md', className = '' }) => {
  const getInitials = (fullName: string): string => {
    const parts = fullName.trim().split(/\s+/);
    const firstInitial = parts[0]?.charAt(0).toUpperCase() || '';
    const lastInitial = parts.length > 1 ? parts[parts.length - 1].charAt(0).toUpperCase() : '';
    return `${firstInitial}${lastInitial}`;
  };

  const sizeConfig = {
    sm: {
      container: 'w-8 h-8',
      text: 'text-xs',
      imageSize: 32,
    },
    md: {
      container: 'w-10 h-10',
      text: 'text-sm',
      imageSize: 40,
    },
    lg: {
      container: 'w-12 h-12',
      text: 'text-base',
      imageSize: 48,
    },
    xl: {
      container: 'w-16 h-16',
      text: 'text-lg',
      imageSize: 64,
    },
  };

  const config = sizeConfig[size];

  if (!user) {
    return (
      <div
        className={`
          ${config.container} rounded-full bg-gray-300 animate-pulse flex-shrink-0
          ${className}
        `}
      />
    );
  }

  const initials = getInitials(user.Username);

  return (
    <div
      className={`
        ${config.container} rounded-full overflow-hidden flex items-center justify-center flex-shrink-0
        ${className}
      `}
    >
      {user.ProfileUrl ? (
        <Image
          src={user.ProfileUrl}
          alt="User icon"
          width={config.imageSize}
          height={config.imageSize}
          className="w-full h-full object-cover"
          onError={(e) => {
            e.currentTarget.style.display = 'none';
          }}
        />
      ) : (
        <div
          className={`
            w-full h-full bg-primary flex 
            items-center justify-center text-white font-semibold 
            ${config.text}
          `}
        >
          {initials}
        </div>
      )}
    </div>
  );
};

export default UserIcon;
