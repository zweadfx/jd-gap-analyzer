// 서비스 운영을 종료했다. 분석 API(Railway)를 내렸기 때문에 입력을 넣어도 결과가 나오지 않는다.
// 메인 페이지를 종료 안내로 대체한다 — 링크를 타고 온 사람이 공고를 붙여넣고 실패하기 전에
// 끝난 서비스라는 것을 먼저 알게 하는 것이 목적이다. 기존 분석 화면은 git 이력에 남아 있다.
//
// 색은 globals.css의 토큰을 그대로 쓴다(--bg/--fg/--blue…). 라이트가 기본이고,
// 저장된 테마가 dark면 layout의 인라인 스크립트가 data-theme를 켜서 이 페이지도 같이 따라간다.
export default function Home() {
  return (
    <main
      style={{
        minHeight: "100vh",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        padding: "48px 20px",
        boxSizing: "border-box",
      }}
    >
      <div style={{ width: "100%", maxWidth: "560px" }}>
        <div
          style={{
            display: "inline-block",
            padding: "6px 12px",
            borderRadius: "999px",
            background: "var(--bg-soft)",
            border: "1px solid var(--line)",
            color: "var(--muted)",
            fontSize: "13px",
            fontWeight: 600,
          }}
        >
          운영 종료
        </div>

        <h1
          style={{
            margin: "20px 0 0",
            fontSize: "clamp(26px, 5vw, 34px)",
            fontWeight: 800,
            lineHeight: 1.35,
            letterSpacing: "-0.02em",
            color: "var(--fg)",
          }}
        >
          갭 분석기는
          <br />
          운영을 종료했습니다.
        </h1>

        <p
          style={{
            margin: "16px 0 0",
            fontSize: "16px",
            lineHeight: 1.7,
            color: "var(--fg-soft)",
          }}
        >
          2026년 7월에 배포해 운영했고, 8월을 끝으로 문을 닫았습니다. 공고와 이력서를 넣어
          분석하는 기능은 더 이상 동작하지 않습니다.
        </p>

        <div
          style={{
            marginTop: "32px",
            padding: "20px",
            borderRadius: "var(--radius)",
            background: "var(--blue-bg)",
            border: "1px solid var(--line)",
          }}
        >
          <p
            style={{
              margin: 0,
              fontSize: "14px",
              fontWeight: 700,
              color: "var(--blue-strong)",
            }}
          >
            코드와 측정 기록은 그대로 있습니다
          </p>
          <p
            style={{
              margin: "8px 0 0",
              fontSize: "14px",
              lineHeight: 1.7,
              color: "var(--fg-soft)",
            }}
          >
            원문 대조 검증기, 실측 강등률, 모델 선정 기준까지 저장소에 공개돼 있습니다.
            CLI로는 지금도 그대로 돌아갑니다.
          </p>
          <a
            href="https://github.com/zweadfx/jd-gap-analyzer"
            style={{
              display: "inline-block",
              marginTop: "14px",
              fontSize: "14px",
              fontWeight: 700,
              color: "var(--blue)",
              textDecoration: "none",
            }}
          >
            GitHub 저장소 보기 →
          </a>
        </div>

        <p
          style={{
            margin: "28px 0 0",
            fontSize: "13px",
            color: "var(--muted)",
          }}
        >
          지원 문서 갭 분석기
        </p>
      </div>
    </main>
  );
}
