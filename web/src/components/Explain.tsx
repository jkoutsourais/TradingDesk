import { CommentDiscussionIcon } from "@primer/octicons-react";
import { useState } from "react";

import { postJson } from "../api";

/** Asks the chief of staff for a plain-English explanation and opens the conversation. */
export function ExplainButton({ id, label = "Explain" }: { id: string; label?: string }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <>
      <button
        className="btn"
        disabled={busy}
        onClick={() => {
          setBusy(true);
          postJson<{ thread_id: string }>("/api/chat", {
            text: "Explain this in plain English: what it is, why the desk made it, and what it means for me.",
            subject_id: id,
          })
            .then(({ thread_id }) => {
              window.location.hash = `#/chat/${thread_id}`;
            })
            .catch((reason: unknown) => setError(String(reason)))
            .finally(() => setBusy(false));
        }}
      >
        <CommentDiscussionIcon size={14} /> {busy ? "Asking" : label}
      </button>
      {error && <span className="neg">{error}</span>}
    </>
  );
}
