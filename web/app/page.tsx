"use client";

import { useEffect, useRef, useState } from "react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

// 백엔드와 같은 값이어야 한다. 다르면 프론트가 통과시킨 입력을 서버가 400으로 거절한다.
const MAX_JOB = 8000;
const MAX_RESUME = 12000;

// 분석에 14~23초 걸린다. 빈 스피너를 20초 보여주면 이탈한다.
// 무엇을 하고 있는지 보여준다. 각 단계는 실제 파이프라인의 Step과 1:1이다.
const STEPS = [
  "공고에서 요구사항을 뽑는 중 (이력서는 아직 보지 않습니다)",
  "이력서에서 근거 문장을 찾는 중",
  "인용문이 이력서 원문에 실제로 있는지 대조하는 중",
  "근거 없는 항목의 보완 방향을 정리하는 중",
];

type Gap = {
  id: string;
  text: string;
  category: string;
  kind: string;
  reason: string;
  bullets?: string[];
};

type Evidence = {
  id: string;
  text: string;
  status: string;
  quote: string | null;
  reason: string;
};

type Result = {
  role_summary: string;
  top_gaps: Gap[];
  other_gaps: Gap[];
  evidence: Evidence[];
  warnings: string[];
  metrics: {
    requirements_count: number;
    quotes_offered: number;
    demoted_count: number;
    evidence_found: number;
    latency_s: number;
    model: string;
  };
};

export default function Page() {
  const [job, setJob] = useState("");
  const [resume, setResume] = useState("");
  const [loading, setLoading] = useState(false);
  const [step, setStep] = useState(0);
  const [elapsed, setElapsed] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<Result | null>(null);
  const resultRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    fetch(`${API}/events/view`, { method: "POST", credentials: "include" }).catch(() => {});
  }, []);

  // 단계 진행은 실제 진행률이 아니라 추정이다. 서버가 단계를 스트리밍하지 않는다.
  // 정직하게 말하면 이것은 연출이다 — 다만 각 문구는 실제로 그 시각에 서버가 하는 일이다.
  //
  // 경계값은 실측에서 왔다 (gpt-4o-mini 전체 23~32초):
  //   Step1 ~9s, Step2 ~8s, 검증 ~0s(코드), Step3 ~6s
  // 추측으로 쓰면 유저가 "다 됐네" 하고 기다리다 배신당한다. 재보고 고칠 것.
  useEffect(() => {
    if (!loading) return;
    const t0 = Date.now();
    const timer = setInterval(() => {
      const s = (Date.now() - t0) / 1000;
      setElapsed(s);
      setStep(s < 10 ? 0 : s < 19 ? 1 : s < 21 ? 2 : 3);
    }, 200);
    return () => clearInterval(timer);
  }, [loading]);

  const jobOver = job.length > MAX_JOB;
  const resumeOver = resume.length > MAX_RESUME;
  const canSubmit = job.trim() && resume.trim() && !jobOver && !resumeOver && !loading;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError(null);
    setResult(null);
    setStep(0);
    setElapsed(0);
    try {
      const res = await fetch(`${API}/analyze`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job, resume }),
      });
      const data = await res.json();
      if (!res.ok) {
        setError(data.error ?? "분석에 실패했습니다.");
      } else {
        setResult(data);
        setTimeout(() => resultRef.current?.scrollIntoView({ behavior: "smooth" }), 60);
      }
    } catch {
      setError("서버에 연결하지 못했습니다. 잠시 후 다시 시도해주세요.");
    } finally {
      setLoading(false);
    }
  }

  const m = result?.metrics;

  return (
    <main className="wrap">
      <h1>공고가 요구하는데, 내 이력서에 없는 것</h1>
      <p className="lede">
        채용 공고와 이력서를 붙여넣으면 <strong>근거가 없는 항목 Top 3</strong>을 찾아줍니다.
      </p>
      <p className="lede">
        이력서를 대신 고쳐주지 않습니다. <strong>무엇이 비어 있는지</strong>만 보여줍니다 — 당신이
        이력서를 보고 5초 만에 맞는지 틀린지 확인할 수 있도록.
      </p>

      <form onSubmit={submit}>
        <div>
          <label htmlFor="job">
            채용 공고
            <span className={`count ${jobOver ? "over" : ""}`}>
              {job.length.toLocaleString()} / {MAX_JOB.toLocaleString()}
            </span>
          </label>
          <textarea
            id="job"
            value={job}
            onChange={(e) => setJob(e.target.value)}
            placeholder="자격요건·우대사항이 포함된 공고 전문을 붙여넣으세요."
          />
        </div>

        <div>
          <label htmlFor="resume">
            내 이력서
            <span className={`count ${resumeOver ? "over" : ""}`}>
              {resume.length.toLocaleString()} / {MAX_RESUME.toLocaleString()}
            </span>
          </label>
          <textarea
            id="resume"
            value={resume}
            onChange={(e) => setResume(e.target.value)}
            placeholder="이력서 전문을 붙여넣으세요. 저장하지 않습니다."
          />
        </div>

        <button type="submit" disabled={!canSubmit}>
          {loading ? "분석 중…" : "갭 찾기"}
        </button>
      </form>

      {(jobOver || resumeOver) && (
        <div className="error">
          입력이 너무 깁니다. 자동으로 잘라내지 않습니다 — 잘린 줄 모르고 엉뚱한 결과를 받는 것이
          더 나쁘기 때문입니다. 직접 줄여주세요.
        </div>
      )}

      {loading && (
        <div className="steps">
          {STEPS.map((s, i) => (
            <div key={i} className={`step ${i === step ? "active" : i < step ? "done" : ""}`}>
              <span className="dot">{i < step ? "✓" : i === step ? "▸" : "·"}</span>
              <span>{s}</span>
            </div>
          ))}
          <div className="elapsed">
            {elapsed.toFixed(0)}초 경과 · 보통 25~35초 걸립니다
            {elapsed > 45 && " · 평소보다 오래 걸리고 있습니다"}
          </div>
        </div>
      )}

      {error && <div className="error">{error}</div>}

      {result && m && (
        <div className="results" ref={resultRef}>
          <div className="role">{result.role_summary}</div>

          <h2>근거 없는 항목 Top 3</h2>
          <p className="sub">필수 &gt; 우대, 기술·경험 &gt; 도메인 &gt; 소프트스킬 순으로 골랐습니다.</p>
          {result.top_gaps.length === 0 && <p className="sub">모든 요구사항에 근거가 있습니다.</p>}
          {result.top_gaps.map((g, i) => (
            <div className="gap" key={g.id}>
              <div className="gap-head">
                <span className="gap-title">
                  {i + 1}. {g.text}
                </span>
                <span className={`tag ${g.category === "필수" ? "req" : ""}`}>{g.category}</span>
              </div>
              <p className="reason">{g.reason}</p>
              {g.bullets && g.bullets.length > 0 && (
                <ul className="bullets">
                  {g.bullets.map((b, j) => (
                    <li key={j}>{b}</li>
                  ))}
                </ul>
              )}
            </div>
          ))}

          {result.other_gaps.length > 0 && (
            <>
              <h2>그 외 근거 없는 항목 ({result.other_gaps.length}개)</h2>
              <p className="sub">
                Top 3만 보여주면 필수 항목이 조용히 사라집니다. 전부 보여줍니다.
              </p>
              {result.other_gaps.map((g) => (
                <div className="row" key={g.id}>
                  <span className={`tag ${g.category === "필수" ? "req" : ""}`}>{g.category}</span>
                  <span>{g.text}</span>
                </div>
              ))}
            </>
          )}

          {result.evidence.length > 0 && (
            <>
              <h2>근거가 있는 항목 ({result.evidence.length}개)</h2>
              <p className="sub">아래 인용문은 이력서 원문과 대조해 실재를 확인한 것입니다.</p>
              {result.evidence.map((e) => (
                <div className="row" key={e.id}>
                  <span className="tag">{e.status}</span>
                  <span>
                    {e.text}
                    {e.quote && <span className="quote">&ldquo;{e.quote}&rdquo;</span>}
                  </span>
                </div>
              ))}
            </>
          )}

          {result.warnings.length > 0 && (
            <div className="warns">
              <strong>검증 경고</strong>
              <ul>
                {result.warnings.map((w, i) => (
                  <li key={i}>{w}</li>
                ))}
              </ul>
            </div>
          )}

          {/* 강등 건수는 항상 표시한다 (컨벤션 1조).
              이 도구가 LLM을 얼마나 못 믿고 있는지 유저가 알아야 한다. */}
          <div className="metrics">
            요구사항 <b>{m.requirements_count}개</b> · 모델이 제시한 인용 <b>{m.quotes_offered}개</b>{" "}
            · 이력서 원문에 없어 <b>버린 인용 {m.demoted_count}개</b> · 근거 확인{" "}
            <b>{m.evidence_found}개</b>
            <br />
            {m.latency_s.toFixed(1)}초 · {m.model}
          </div>
        </div>
      )}

      <p className="note">
        이력서는 저장하지 않습니다. 분석에만 쓰이고 서버에 파일로 남지 않습니다.
        <br />
        LLM은 그럴듯한 인용문을 지어냅니다. 그래서 모델이 준 인용문을 이력서 원문과 글자 단위로
        대조해, 원문에 없으면 근거로 인정하지 않고 버립니다. 위에 표시된 &lsquo;버린 인용&rsquo;
        건수가 그것입니다.
      </p>
    </main>
  );
}
