import type { Metadata } from "next";
import "./globals.css";
import ThemeToggle from "./theme-toggle";

// 커뮤니티에 링크가 올라가면 사람들이 처음 보는 건 이 카드다. 유입의 첫 관문.
const SITE = "https://jd-gap-zweadfxs-projects.vercel.app";
const OG_TITLE = "공고는 요구하는데, 내 서류엔 없는 것 3가지";
const OG_DESC =
  "채용공고와 이력서를 붙여넣으면, 공고가 요구하지만 서류에 근거가 없는 항목 Top 3를 원문 인용과 함께 찾아드립니다. 무료, 저장 안 함.";

export const metadata: Metadata = {
  metadataBase: new URL(SITE),
  title: "지원 문서 갭 분석기 — 공고는 요구하는데 내 서류엔 없는 것",
  description: OG_DESC,
  openGraph: {
    type: "website",
    locale: "ko_KR",
    url: SITE,
    siteName: "지원 문서 갭 분석기",
    title: OG_TITLE,
    description: OG_DESC,
    images: [{ url: "/og.png", width: 1200, height: 630, alt: OG_TITLE }],
  },
  twitter: {
    card: "summary_large_image",
    title: OG_TITLE,
    description: OG_DESC,
    images: ["/og.png"],
  },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ko">
      <head>
        {/* 렌더 전에 저장된 테마를 적용해 다크 선택자의 깜빡임(FOUC)을 막는다.
            기본은 라이트 — 저장값이 'dark'일 때만 data-theme를 켠다. */}
        <script
          dangerouslySetInnerHTML={{
            __html:
              "try{if(localStorage.getItem('jd_theme')==='dark')document.documentElement.setAttribute('data-theme','dark');}catch(e){}",
          }}
        />
      </head>
      <body>
        <ThemeToggle />
        {children}
      </body>
    </html>
  );
}
