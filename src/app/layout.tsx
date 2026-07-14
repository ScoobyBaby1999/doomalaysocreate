import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";
import { Toaster } from "@/components/ui/toaster";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "doomalaysocreate — Multi-Model AI Panel",
  description: "Fan your prompt out to a diverse panel of frontier LLMs across providers. Merge their uncorrelated opinions. Bring your own keys.",
  keywords: ["AI panel", "judge panel", "LLM", "multi-model", "critique", "frontier models", "bring your own keys"],
  authors: [{ name: "doomalaysocreate" }],
  icons: {
    icon: "https://z-cdn.chatglm.cn/z-ai/static/logo.svg",
  },
  openGraph: {
    title: "doomalaysocreate — Multi-Model AI Panel",
    description: "One panel. Every frontier model. Millions of spaces.",
    siteName: "doomalaysocreate",
    type: "website",
  },
  twitter: {
    card: "summary_large_image",
    title: "doomalaysocreate",
    description: "Multi-model judge panel with bring-your-own-keys.",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body
        className={`${geistSans.variable} ${geistMono.variable} antialiased bg-background text-foreground`}
      >
        {children}
        <Toaster />
      </body>
    </html>
  );
}
