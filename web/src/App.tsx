import {
  BriefcaseIcon,
  CommentDiscussionIcon,
  GraphIcon,
  HomeIcon,
  LightBulbIcon,
  ServerIcon,
  TrophyIcon,
  WorkflowIcon,
} from "@primer/octicons-react";
import type { Icon } from "@primer/octicons-react";
import { lazy, Suspense, useMemo } from "react";

import { ErrorBoundary } from "./components/ErrorBoundary";
import { Palette, type Command } from "./components/Palette";
import { StatusBar } from "./components/StatusBar";
import { useHashRoute } from "./hooks";
import { Book } from "./views/Book";
import { Chat } from "./views/Chat";
import { Desks } from "./views/Desks";
import { PlanView } from "./views/Plan";
import { ThesisList, ThesisView } from "./views/Theses";
import { BriefView, Today } from "./views/Today";

// Lanes and Trace carry React Flow and ECharts; loading them on demand keeps the first
// page light on a phone.
const Lanes = lazy(() => import("./views/Lanes").then((m) => ({ default: m.Lanes })));
const Trace = lazy(() => import("./views/Trace").then((m) => ({ default: m.Trace })));
const Scores = lazy(() => import("./views/Scores").then((m) => ({ default: m.Scores })));

interface Tab {
  id: string;
  title: string;
  icon: Icon;
}

const TABS: Tab[] = [
  { id: "today", title: "Today", icon: HomeIcon },
  { id: "lanes", title: "Lanes", icon: WorkflowIcon },
  { id: "desks", title: "Desks", icon: ServerIcon },
  { id: "trace", title: "Trace", icon: GraphIcon },
  { id: "theses", title: "Theses", icon: LightBulbIcon },
  { id: "book", title: "Book", icon: BriefcaseIcon },
  { id: "scores", title: "Scores", icon: TrophyIcon },
  { id: "chat", title: "Chat", icon: CommentDiscussionIcon },
];

const DESKS: [string, string][] = [
  ["data", "Data"],
  ["watch", "Watch"],
  ["research", "Research"],
  ["factcheck", "Fact-check"],
  ["idea", "Idea"],
  ["holdings", "Holdings"],
  ["analyst", "Analyst"],
  ["trader", "Trader"],
  ["risk", "Risk"],
  ["front_office", "Front office"],
  ["scoring", "Scoring"],
];

function go(hash: string): void {
  window.location.hash = hash;
}

function View({ route }: { route: string[] }) {
  const [view, id] = route;
  switch (view) {
    case "desks":
      return <Desks selected={id} />;
    case "lanes":
      return <Lanes />;
    case "trace":
    case "alerts":
      return <Trace id={id} />;
    case "briefs":
      return id && id !== "latest" ? <BriefView id={id} /> : <Today />;
    case "theses":
      return id ? <ThesisView id={id} /> : <ThesisList />;
    case "plans":
      return id ? <PlanView id={id} /> : <Today />;
    case "book":
      return <Book />;
    case "scores":
      return <Scores />;
    case "chat":
      return <Chat threadId={id} />;
    default:
      return <Today />;
  }
}

export function App() {
  const route = useHashRoute();
  const active = route[0] ?? "today";
  const commands = useMemo<Command[]>(
    () => [
      ...TABS.map((tab) => ({ id: `tab-${tab.id}`, label: tab.title, hint: "view", run: () => go(`#/${tab.id}`) })),
      ...DESKS.map(([id, title]) => ({ id: `desk-${id}`, label: `${title} desk`, hint: "desk", run: () => go(`#/desks/${id}`) })),
    ],
    [],
  );
  return (
    <div className="app">
      <nav className="sidebar">
        <h6>Views</h6>
        {TABS.map((tab) => (
          <a key={tab.id} href={`#/${tab.id}`} className={active === tab.id && !(tab.id === "desks" && route[1]) ? "active" : ""}>
            <tab.icon size={16} /> {tab.title}
          </a>
        ))}
        <h6>Desks</h6>
        {DESKS.map(([id, title]) => (
          <a key={id} href={`#/desks/${id}`} className={active === "desks" && route[1] === id ? "active" : ""}>
            {title}
          </a>
        ))}
      </nav>
      <main className="main">
        <div className="tabs">
          {TABS.map((tab) => (
            <a key={tab.id} href={`#/${tab.id}`} className={active === tab.id ? "active" : ""}>
              {tab.title}
            </a>
          ))}
          <span style={{ flex: 1 }} />
          <a href="#" onClick={(e) => { e.preventDefault(); window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", ctrlKey: true })); }} className="muted">
            Ctrl+K
          </a>
        </div>
        {/* Keyed by route so a failed view resets when Jon navigates away. */}
        <ErrorBoundary key={route.join("/")}>
          <Suspense fallback={<div className="content"><div className="empty">Loading</div></div>}>
            <View route={route} />
          </Suspense>
        </ErrorBoundary>
      </main>
      <StatusBar />
      <Palette commands={commands} />
    </div>
  );
}
