import type { Metadata } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "memory-system — chat",
  description: "M2.5 thin checkpoint: a chat UI over the memory-system backend.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
