import type { Metadata } from "next";
import InterviewFlow from "@/components/interview/InterviewFlow";

export const metadata: Metadata = {
  title: "Practice Interview — AI Interview Coach",
  description: "Live voice-first practice interview with adaptive AI questions, camera presence, and criterion evaluation.",
};

export default function InterviewPage() {
  return (
    <main id="content" style={{ maxWidth: "84rem" }}>
      <InterviewFlow />
    </main>
  );
}
