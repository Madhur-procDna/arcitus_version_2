'use client';
import React from 'react';
import Image from 'next/image';
import { useRouter, usePathname } from 'next/navigation';
import UserIcon from './UserIcon';
import { MOCK_USER } from '@/utils/constants';
import { useAuth } from '@/context/AuthContext';

const Navbar = () => {
  const router = useRouter();
  const pathname = usePathname();
  const { logout, isAuthenticated } = useAuth();

  const handleLogoClick = () => {
    router.push('/');
  };

  const handleLogout = () => {
    logout();
  };

  const isLoginPage = pathname === '/login';

  return (
    <div className="flex items-center justify-between w-full h-full py-4 px-4 md:px-4 lg:px-6 bg-white">
      <div className="flex items-center gap-4">
        <button className="cursor-pointer" onClick={handleLogoClick}>
          <Image
            src="/Images/ProcDNALogo.svg"
            alt="ProcDNA Logo"
            width={120}
            height={32}
            priority
            className="h-8 lg:h-10 w-auto flex-shrink-0"
          />
        </button>

        <span className="text-[#f2d322] text-2xl leading-none select-none" aria-hidden="true">
          |
        </span>
        <div className="flex items-center justify-center">
          <Image
            src="/Images/arcttis logo.png"
            alt="Arcutis"
            width={132}
            height={32}
            className="h-7 lg:h-8 w-auto"
          />
        </div>
      </div>

      {isAuthenticated && !isLoginPage && (
        <div className="flex items-center gap-4">
          <UserIcon user={MOCK_USER} size="md" />
          <button
            onClick={handleLogout}
            className="cursor-pointer p-3 rounded-full hover:bg-gray-100 hover:shadow-md transition-colors"
            aria-label="Logout"
          >
            <Image
              src="/Images/Logout.svg"
              alt="Logout"
              width={20}
              height={20}
              className="w-5 h-5"
            />
          </button>
        </div>
      )}
    </div>
  );
};

export default Navbar;
