import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Mission Control — homelab autopilot",
  description: "自律運用エージェントのライブ管制画面",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="ja">
      <body>{children}</body>
    </html>
  );
}

