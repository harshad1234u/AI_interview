import Link from "next/link";

export default function LandingPage() {
  return (
    <div className="landing-container">
      {/* Hero Section */}
      <section className="landing-hero" aria-labelledby="hero-heading">
        <span className="badge badge-primary">Next-Gen AI Interview Coaching</span>
        <h1 id="hero-heading" className="landing-tagline">
          Master High-Stakes Job Interviews with <span>Voice-First AI</span>
        </h1>
        <p className="landing-sub">
          Experience realistic technical and behavioral interviews in a video-conference environment.
          Speak naturally, receive real-time expression awareness, and get actionable criterion-based feedback.
        </p>
        <div className="landing-cta-group">
          <Link href="/login" className="button-link primary">
            Start Practicing Free →
          </Link>
          <a href="#preview" className="button-link">
            Explore Interview Room
          </a>
        </div>
      </section>

      {/* Realistic Product Interface Preview */}
      <section id="preview" className="preview-window" aria-label="Product Interface Preview">
        <div className="preview-window-bar">
          <span className="preview-dot" />
          <span className="preview-dot" />
          <span className="preview-dot" />
          <span className="preview-window-title">AI Interview Coach — Live Video Session · Software Engineer</span>
        </div>
        <div className="preview-body-mock">
          {/* Left Panel Mock */}
          <div className="preview-sidebar-mock">
            <span className="badge badge-primary" style={{ alignSelf: "flex-start" }}>Topic 2 of 4 · Web Architecture</span>
            <h4 style={{ fontSize: "0.95rem", lineHeight: 1.4 }}>
              &quot;How does the Virtual DOM optimize rendering performance in complex SPAs?&quot;
            </h4>
            <div style={{ marginTop: "auto", display: "flex", flexDirection: "column", gap: "0.4rem" }}>
              <span className="hint" style={{ fontSize: "0.75rem" }}>Past Questions:</span>
              <div style={{ padding: "0.4rem 0.6rem", background: "var(--surface-2)", borderRadius: "6px", fontSize: "0.75rem" }}>
                ✓ Q1: REST vs GraphQL APIs (Answered)
              </div>
            </div>
          </div>

          {/* Center Stage Mock */}
          <div className="preview-stage-mock">
            <div className="preview-video-box">
              <span className="live-indicator" style={{ position: "absolute", top: "10px", left: "10px" }}>
                ● Live Camera Preview
              </span>
              <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "0.4rem" }}>
                <span style={{ fontSize: "2rem" }}>👤</span>
                <span style={{ fontSize: "0.85rem", opacity: 0.9 }}>Candidate Video Feed Active</span>
              </div>
              <div style={{
                position: "absolute", bottom: "10px", left: "10px", right: "10px",
                display: "flex", justifyContent: "space-between", fontSize: "0.75rem",
                background: "rgba(0,0,0,0.6)", padding: "4px 8px", borderRadius: "6px"
              }}>
                <span>Face Expression: <strong>Engaged &amp; Confident</strong></span>
                <span>Mic: <strong>Listening</strong></span>
              </div>
            </div>

            {/* Voice Controls Mock */}
            <div style={{ display: "flex", alignItems: "center", gap: "0.75rem" }}>
              <span className="badge badge-success">🎙 Voice Mode Active</span>
              <div className="waveform-container waveform-active" aria-hidden="true">
                <span className="waveform-bar" />
                <span className="waveform-bar" />
                <span className="waveform-bar" />
                <span className="waveform-bar" />
                <span className="waveform-bar" />
              </div>
              <span className="hint" style={{ fontSize: "0.85rem" }}>&quot;In React, state changes produce a new virtual tree...&quot;</span>
            </div>
          </div>
        </div>
      </section>

      {/* Highlights / Stats Row */}
      <section className="dashboard-stats-grid" aria-label="Platform Highlights">
        <div className="stat-card">
          <span className="stat-label">Engineering Roles</span>
          <span className="stat-value">30+</span>
          <span className="stat-hint">Frontend, Backend, AI/ML, Cloud, DevOps</span>
        </div>
        <div className="stat-card">
          <span className="stat-label">Privacy Guarantee</span>
          <span className="stat-value">100%</span>
          <span className="stat-hint">Edge vision &amp; audio — no media stored</span>
        </div>
        <div className="stat-card">
          <span className="stat-label">Feedback Latency</span>
          <span className="stat-value">Instant</span>
          <span className="stat-hint">Answer evaluation and quote extraction</span>
        </div>
        <div className="stat-card">
          <span className="stat-label">Evaluation Rubric</span>
          <span className="stat-value">9 Criteria</span>
          <span className="stat-hint">Relevance, structure, depth &amp; reasoning</span>
        </div>
      </section>

      {/* Key Features Grid */}
      <section id="features" aria-labelledby="features-heading">
        <div className="landing-section-header">
          <span className="eyebrow">Enterprise-Grade Features</span>
          <h2 id="features-heading">Engineered for Realistic Interview Preparation</h2>
          <p className="hint">
            Every component is built to replicate the dynamics, pacing, and rigor of a real hiring interview.
          </p>
        </div>

        <div className="features-grid" style={{ marginTop: "2rem" }}>
          <div className="feature-card">
            <div className="feature-icon">🎙</div>
            <h3>Voice-First Conversational AI</h3>
            <p className="hint">
              Questions are read aloud by synthetic voices and candidate answers are transcribed in real-time.
              Speak naturally without having to type lengthy essays.
            </p>
          </div>

          <div className="feature-card">
            <div className="feature-icon">📹</div>
            <h3>Real-Time Camera &amp; Vision Tracking</h3>
            <p className="hint">
              Integrated webcam monitoring powered by edge neural networks classifies expressions and composure
              without sending a single video frame to any remote server.
            </p>
          </div>

          <div className="feature-card">
            <div className="feature-icon">🎯</div>
            <h3>Resume Fact Extraction &amp; Alignment</h3>
            <p className="hint">
              Upload your resume in PDF or DOCX format. The platform automatically extracts your skills, projects,
              and experience, generating personalized technical questions based on your background.
            </p>
          </div>

          <div className="feature-card">
            <div className="feature-icon">📊</div>
            <h3>Criterion-Based Scoring Rubrics</h3>
            <p className="hint">
              Post-interview reports score relevance, completeness, communication clarity, and reasoning depth.
              Direct quotes show exactly what you stated well and where gaps exist.
            </p>
          </div>

          <div className="feature-card">
            <div className="feature-icon">🔄</div>
            <h3>Targeted Re-Practice &amp; Retry</h3>
            <p className="hint">
              Identified weak spots turn into focused retry sessions. Retake targeted questions without losing
              historical attempt records or skewing previous analytics.
            </p>
          </div>

          <div className="feature-card">
            <div className="feature-icon">📄</div>
            <h3>Document Export (HTML &amp; Markdown)</h3>
            <p className="hint">
              Download clean, beautifully formatted debrief documents of your interview questions, answers,
              and evaluations to review offline or share with mentors.
            </p>
          </div>
        </div>
      </section>

      {/* How It Works Road */}
      <section id="how-it-works" aria-labelledby="steps-heading">
        <div className="landing-section-header">
          <span className="eyebrow">Step-by-Step Methodology</span>
          <h2 id="steps-heading">From Profile Setup to Interview Mastery</h2>
        </div>

        <div className="steps-grid" style={{ marginTop: "2rem" }}>
          <div className="step-card">
            <div className="step-number">1</div>
            <h4>Build Profile &amp; Facts</h4>
            <p className="hint">Upload your resume or manually input your key projects, technologies, and certifications.</p>
          </div>
          <div className="step-card">
            <div className="step-number">2</div>
            <h4>Configure Interview</h4>
            <p className="hint">Choose your target engineering role, difficulty tier, duration (15–45m), and AI assistance mode.</p>
          </div>
          <div className="step-card">
            <div className="step-number">3</div>
            <h4>Speak in the Live Room</h4>
            <p className="hint">Participate in a focused video session with AI audio dialogue, camera preview, and adaptive questions.</p>
          </div>
          <div className="step-card">
            <div className="step-number">4</div>
            <h4>Analyze &amp; Improve</h4>
            <p className="hint">Review score breakdowns, read verbatim quotes, and launch targeted retry drills on weaker topics.</p>
          </div>
        </div>
      </section>

      {/* Benefits Section */}
      <section id="benefits" className="card" style={{ padding: "2.5rem 2rem", textAlign: "center" }}>
        <span className="eyebrow">Candidate Advantage</span>
        <h2 style={{ margin: "0.5rem 0 1rem" }}>Gain Real Interview Confidence Before the Actual Call</h2>
        <p className="lead" style={{ margin: "0 auto 1.5rem" }}>
          Ditch static flashcards and leetcode grind alone. Practice conversational articulation under authentic
          time constraints, eliminate verbal fillers, and learn to structure high-scoring answers.
        </p>
        <Link href="/login" className="button-link primary" style={{ padding: "0.75rem 1.75rem", fontSize: "1rem" }}>
          Create Your Practice Profile Now →
        </Link>
      </section>

      {/* Footer */}
      <footer className="landing-footer">
        <div className="landing-footer-top">
          <div className="brand">
            <span className="brand-mark" aria-hidden="true">AI</span>
            <span>Interview Coach</span>
          </div>
          <div style={{ display: "flex", gap: "1.5rem", flexWrap: "wrap" }}>
            <a href="#features">Features</a>
            <a href="#how-it-works">How It Works</a>
            <a href="#benefits">Candidate Benefits</a>
            <Link href="/login">Sign In</Link>
          </div>
        </div>
        <div className="landing-footer-bottom">
          <span>Practice platform for skills development. No hiring decisions or personal data monetization.</span>
          <span>© {new Date().getFullYear()} AI Interview Coach. All rights reserved.</span>
        </div>
      </footer>
    </div>
  );
}
