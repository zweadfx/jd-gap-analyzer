import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "지원 문서 갭 분석기 — 공고가 요구하는데 내 문서에 없는 것",
  description:
    "채용 공고와 이력서(또는 포트폴리오)를 붙여넣으면, 공고가 요구하는데 근거가 없는 항목 Top 3을 찾아줍니다. 첨삭하지 않습니다. 인용문은 원문과 대조해 검증합니다.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ko">
      <body>{children}</body>
    </html>
  );
}
