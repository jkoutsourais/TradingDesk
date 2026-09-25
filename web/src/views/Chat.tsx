import { CommentDiscussionIcon, LightBulbIcon, PlusIcon } from "@primer/octicons-react";
import { useEffect, useRef, useState } from "react";

import { getJson, postJson } from "../api";
import { ago, time } from "../format";
import { useApi } from "../hooks";

interface ThreadRow {
  thread_id: string;
  started_at: string;
  last_at: string;
  first_text: string;
  messages: number;
  replies: number;
}

interface ThreadItem {
  id: string;
  role: "jon" | "desk";
  text: string;
  mode?: string;
  subject_id?: string | null;
  status?: string;
  error?: string | null;
  cited?: string[];
  created_at: string;
  answered?: boolean;
}

interface ThreadData {
  thread_id: string;
  items: ThreadItem[];
  pending: boolean;
}

interface IntakeState {
  status: "pending" | "drafted" | "failed";
  draft_id?: string;
  draft?: {
    statement: string;
    instruments: string[];
    direction: string;
    horizon: string | null;
    conviction: number | null;
    invalidation: { hard: { level: string }; warning: { level: string } } | null;
  };
  missing?: string[];
  questions?: string[];
  error?: string;
}

function Conversation({ threadId }: { threadId: string }) {
  const { data, reload } = useApi<ThreadData>(`/api/chat/threads/${threadId}`, ["chat_reply", "chat_message"], 30_000);
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!data?.pending) return;
    const id = window.setInterval(reload, 4000);
    return () => window.clearInterval(id);
  }, [data?.pending, reload]);
  useEffect(() => end.current?.scrollIntoView({ block: "end" }), [data?.items.length]);
  if (!data) return <div className="empty">Loading</div>;
  return (
    <div style={{ display: "grid", gap: 8, padding: 12 }}>
      {data.items.map((item) => (
        <div
          key={item.id}
          className="box"
          style={{
            justifySelf: item.role === "jon" ? "end" : "start",
            maxWidth: "88%",
            background: item.role === "jon" ? "var(--bgColor-muted)" : "var(--bgColor-default)",
          }}
        >
          <div className="box-body" style={{ whiteSpace: "pre-wrap" }}>
            {item.role === "jon" && item.mode === "explain" && item.subject_id && (
              <div className="muted" style={{ fontSize: 12 }}>
                Explaining <a href={`#/trace/${item.subject_id}`}>this artifact</a>
              </div>
            )}
            {item.text}
            <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>
              {item.role === "desk" ? "Chief of staff" : "Jon"} · {time(item.created_at)}
              {item.status === "failed" && item.error ? ` · ${item.error}` : ""}
              {item.cited?.map((id, i) => (
                <a key={id} href={`#/trace/${id}`} style={{ marginLeft: 6 }}>
                  [{i + 1}]
                </a>
              ))}
            </div>
          </div>
        </div>
      ))}
      {data.pending && (
        <div className="muted">
          The chief of staff is answering. Replies wait while a shift has the GPU.
        </div>
      )}
      <div ref={end} />
    </div>
  );
}

function IntakePanel({ state, onAnswer, onConfirm }: { state: IntakeState; onAnswer: (text: string) => void; onConfirm: () => void }) {
  const [answer, setAnswer] = useState("");
  if (state.status === "pending") return <div className="box-body muted">Drafting the thesis from your message (about a minute outside shifts).</div>;
  if (state.status === "failed") return <div className="box-body neg">Drafting failed: {state.error}. Restate the idea and try again.</div>;
  const draft = state.draft;
  if (!draft) return null;
  return (
    <div className="box-body" style={{ display: "grid", gap: 6 }}>
      <div>
        <b>{draft.instruments.join(", ")}</b> {draft.direction}: {draft.statement}
      </div>
      <div className="muted mono">
        horizon {draft.horizon ?? "?"} · conviction {draft.conviction ?? "?"} · hard {draft.invalidation?.hard.level ?? "?"} · warning{" "}
        {draft.invalidation?.warning.level ?? "?"}
      </div>
      {state.questions && state.questions.length > 0 ? (
        <>
          <ul className="lines">{state.questions.map((q) => <li key={q}>{q}</li>)}</ul>
          <form
            style={{ display: "flex", gap: 6 }}
            onSubmit={(e) => {
              e.preventDefault();
              if (answer.trim()) onAnswer(answer.trim());
              setAnswer("");
            }}
          >
            <input className="input" value={answer} onChange={(e) => setAnswer(e.target.value)} placeholder="Answer the questions" />
            <button className="btn primary" type="submit">Send</button>
          </form>
        </>
      ) : (
        <div>
          <button className="btn primary" onClick={onConfirm}>Confirm thesis</button>{" "}
          <span className="muted">It becomes active and joins the next shift's research and debate.</span>
        </div>
      )}
    </div>
  );
}

export function Chat({ threadId }: { threadId?: string }) {
  const threads = useApi<ThreadRow[]>("/api/chat/threads", ["chat_reply", "chat_message"]);
  const [mode, setMode] = useState<"ask" | "thesis">("ask");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [intakeMessage, setIntakeMessage] = useState<string | null>(null);
  const [intake, setIntake] = useState<IntakeState | null>(null);
  const [confirmed, setConfirmed] = useState<string | null>(null);

  useEffect(() => {
    if (!intakeMessage) return;
    let stop = false;
    const poll = () =>
      getJson<IntakeState>(`/intake/messages/${intakeMessage}`)
        .then((state) => {
          if (stop) return;
          setIntake(state);
          if (state.status === "pending") window.setTimeout(poll, 3000);
        })
        .catch((reason: unknown) => setError(String(reason)));
    poll();
    return () => {
      stop = true;
    };
  }, [intakeMessage]);

  const sendIntake = (body: { text: string; draft_id?: string }) =>
    postJson<{ message_id: string }>("/intake/messages", body).then(({ message_id }) => {
      setIntake({ status: "pending" });
      setIntakeMessage(message_id);
    });

  const send = () => {
    const value = text.trim();
    if (!value) return;
    setBusy(true);
    setError(null);
    const request =
      mode === "ask"
        ? postJson<{ thread_id: string }>("/api/chat", { text: value, thread_id: threadId ?? null }).then(({ thread_id }) => {
            window.location.hash = `#/chat/${thread_id}`;
            threads.reload();
          })
        : sendIntake({ text: value }).then(() => setConfirmed(null));
    request
      .then(() => setText(""))
      .catch((reason: unknown) => setError(String(reason)))
      .finally(() => setBusy(false));
  };

  return (
    <div className="content chat-layout">
      <div className="box">
        <div className="box-header">
          <CommentDiscussionIcon /> Conversations
          <span className="spacer" />
          <a className="btn" href="#/chat" title="New conversation"><PlusIcon size={14} /></a>
        </div>
        {(threads.data ?? []).map((t) => (
          <a
            key={t.thread_id}
            href={`#/chat/${t.thread_id}`}
            className="box-body"
            style={{ display: "block", borderBottom: "1px solid var(--borderColor-muted)", background: t.thread_id === threadId ? "var(--bgColor-muted)" : undefined, color: "var(--fgColor-default)" }}
          >
            <div style={{ whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{t.first_text}</div>
            <div className="muted" style={{ fontSize: 11 }}>{ago(t.last_at)} · {t.messages} asked</div>
          </a>
        ))}
        {threads.data?.length === 0 && <div className="empty">No conversations yet.</div>}
      </div>
      <div className="box">
        <div className="box-header">
          <div className="tabs" style={{ position: "static", padding: 0, border: 0 }}>
            <a href="#" className={mode === "ask" ? "active" : ""} onClick={(e) => { e.preventDefault(); setMode("ask"); }}>
              <CommentDiscussionIcon size={14} /> Ask the desk
            </a>
            <a href="#" className={mode === "thesis" ? "active" : ""} onClick={(e) => { e.preventDefault(); setMode("thesis"); }}>
              <LightBulbIcon size={14} /> New thesis
            </a>
          </div>
        </div>
        {mode === "ask" ? (
          threadId ? <Conversation threadId={threadId} /> : (
            <div className="empty">
              Ask about holdings, theses, plans, vetoes, scores or today's news. Answers come from the desk's own records, with links to the sources.
            </div>
          )
        ) : intake ? (
          confirmed ? (
            <div className="box-body">Thesis confirmed. <a href={`#/theses/${confirmed}`}>Open it</a>.</div>
          ) : (
            <IntakePanel
              state={intake}
              onAnswer={(answer) => {
                if (intake.draft_id) sendIntake({ text: answer, draft_id: intake.draft_id }).catch((r: unknown) => setError(String(r)));
              }}
              onConfirm={() => {
                if (!intake.draft_id) return;
                postJson<{ thesis_id: string }>(`/intake/drafts/${intake.draft_id}/confirm`, {})
                  .then(({ thesis_id }) => setConfirmed(thesis_id))
                  .catch((r: unknown) => setError(String(r)));
              }}
            />
          )
        ) : (
          <div className="empty">
            State your idea in your own words: the instrument, which way, why, how long, where you would be wrong and how confident you are. Numbers you give are kept exactly; missing pieces are asked for.
          </div>
        )}
        <form
          className="box-body"
          style={{ display: "flex", gap: 6, borderTop: "1px solid var(--borderColor-default)" }}
          onSubmit={(e) => {
            e.preventDefault();
            send();
          }}
        >
          <textarea
            className="input"
            rows={2}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
            placeholder={mode === "ask" ? "Ask the desk" : "Describe the thesis"}
          />
          <button className="btn primary" type="submit" disabled={busy}>
            {busy ? "Sending" : "Send"}
          </button>
        </form>
        {error && <div className="box-body neg">{error}</div>}
      </div>
    </div>
  );
}
