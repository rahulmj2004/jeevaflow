import type { Journey } from "../types";

const STEPS = [
  "Document",
  "Evidence",
  "Timeline",
  "Instruction",
  "Open loop",
  "WhatsApp",
  "New evidence",
  "Potential match",
  "Human confirms",
  "Closed",
] as const;

/**
 * Shows how far the patient's journey has progressed through the
 * JeevaFlow story, derived only from real API data.
 */
export function StoryStrip({ journey }: { journey: Journey }) {
  const hasDocument = journey.documents.some((doc) => doc.status === "PROCESSED");
  const hasEvidence = journey.timeline.some((item) => item.evidence);
  const hasInstruction = journey.timeline.some((item) => item.type === "COMMITMENT");
  const whatsappDocs = journey.documents.filter((doc) => doc.source === "WHATSAPP" && doc.status === "PROCESSED");
  const whatsappEvidence = journey.timeline.some((item) => item.evidence?.document_source === "WHATSAPP");
  const anyMatch = journey.open_loops.some((loop) => loop.potential_matches.length > 0 || loop.confirmed_match);
  const confirmed = journey.open_loops.some((loop) => loop.confirmed_match);

  const done = [
    hasDocument,
    hasEvidence,
    journey.timeline.length > 0,
    hasInstruction,
    journey.open_loops.length > 0,
    whatsappDocs.length > 0,
    whatsappEvidence,
    anyMatch,
    confirmed,
    confirmed,
  ];

  const current = done.findIndex((value) => !value);

  return (
    <nav className="story" aria-label="Journey progress">
      <ol>
        {STEPS.map((step, index) => (
          <li
            key={step}
            className={done[index] ? "story-done" : index === current ? "story-current" : "story-todo"}
            aria-current={index === current ? "step" : undefined}
          >
            <span className="story-dot" aria-hidden="true">
              {done[index] ? "✓" : index + 1}
            </span>
            <span className="story-label">{step}</span>
          </li>
        ))}
      </ol>
    </nav>
  );
}
