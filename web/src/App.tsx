import { GraphIcon, HomeIcon, ServerIcon } from "@primer/octicons-react";
import type { Icon } from "@primer/octicons-react";
import { useMemo } from "react";

import { Palette, type Command } from "./components/Palette";
import { StatusBar } from "./components/StatusBar";
import { useHashRoute } from "./hooks";
import { Desks } from "./views/Desks";
import { Today } from "./views/Today";
import { Trace } from "./views/Trace";

interface Tab {
  id: string;
  title: string;
  icon: Icon;
}

const TABS: Tab[] = [
  { id: "today", title: "Today", icon: HomeIcon },
  { id: "desks", title: "Desks", icon: ServerIcon },
  { id: "trace", title: "Trace", icon: GraphIcon },
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
    case "trace":
    case "theses":
    case "plans":
    case "briefs":
    case "alerts":
      return <Trace id={id} />;
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
        <View route={route} />
      </main>
      <StatusBar />
      <Palette commands={commands} />
    </div>
  );
}
