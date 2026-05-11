import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";
import Navbar from "@/components/Navbar";
import { AuthProvider } from "@/context/AuthContext";

const inter = Inter({ subsets: ["latin"] });

export const metadata: Metadata = {
  title: "Arcutis",
  description: "Takeda SDA assistant",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className={inter.className}>
        <AuthProvider>
          <div className="flex flex-col h-screen">
            {/* Navbar at top */}
            <div className="h-16 flex-shrink-0 border-b border-gray-200">
              <Navbar />
            </div>
            {/* Main content area */}
            <div className="flex-1 overflow-hidden">
              {children}
            </div>
          </div>
        </AuthProvider>
      </body>
    </html>
  );
}
