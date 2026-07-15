"use client";

// 다크모드 토글. page.tsx(기능 코드)를 건드리지 않으려고 별도 컴포넌트로 뒀다.
// 기본은 라이트. 사용자가 켜면 localStorage에 저장하고, 다음 방문 때 layout의
// 인라인 스크립트가 렌더 전에 data-theme를 세팅해 깜빡임(FOUC)을 막는다.
import { useEffect, useState } from "react";

export default function ThemeToggle() {
  const [dark, setDark] = useState(false);

  useEffect(() => {
    setDark(document.documentElement.getAttribute("data-theme") === "dark");
  }, []);

  function toggle() {
    const next = !dark;
    setDark(next);
    document.documentElement.setAttribute("data-theme", next ? "dark" : "light");
    try {
      localStorage.setItem("jd_theme", next ? "dark" : "light");
    } catch {
      /* localStorage 불가 환경이면 이번 세션만 적용된다 */
    }
  }

  return (
    <button
      type="button"
      className="theme-toggle"
      onClick={toggle}
      aria-label={dark ? "라이트 모드로 전환" : "다크 모드로 전환"}
    >
      {dark ? "☀ 라이트" : "☾ 다크"}
    </button>
  );
}
