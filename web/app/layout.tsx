import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "이력서 갭 분석기 — 공고가 요구하는데 내 이력서에 없는 것",
  description:
    "채용 공고와 이력서를 붙여넣으면, 공고가 요구하는데 이력서에 근거가 없는 항목 Top 3을 찾아줍니다. 인용문은 이력서 원문과 대조해 검증합니다.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ko">
      <body>{children}</body>
    </html>
  );
}
