import type { Metadata } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "Banking Assistant",
  description: "Your accounts, cards and loans in a helpful banking conversation.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
