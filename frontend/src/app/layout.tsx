import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "Outreach Agent", template: "%s · Outreach Agent" },
  description: "Evidence-based prospect research and outreach",
  robots: { index: false, follow: false },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="min-h-full">{children}</body>
    </html>
  );
}
