import { SearchIcon } from "@primer/octicons-react";
import { useEffect, useMemo, useRef, useState } from "react";

export interface Command {
  id: string;
  label: string;
  hint?: string;
  run: () => void;
}

export function Palette({ commands }: { commands: Command[] }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [index, setIndex] = useState(0);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setOpen((value) => !value);
        setQuery("");
        setIndex(0);
      } else if (event.key === "Escape") {
        setOpen(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    if (open) input.current?.focus();
  }, [open]);

  const matches = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return commands.filter((c) => !needle || c.label.toLowerCase().includes(needle)).slice(0, 20);
  }, [commands, query]);

  if (!open) return null;
  const choose = (command: Command | undefined) => {
    if (!command) return;
    setOpen(false);
    command.run();
  };
  return (
    <div className="palette-backdrop" onClick={() => setOpen(false)}>
      <div className="palette" onClick={(event) => event.stopPropagation()}>
        <div style={{ display: "flex", alignItems: "center", paddingLeft: 12 }}>
          <SearchIcon />
          <input
            ref={input}
            className="input"
            placeholder="Go to a view, desk or thesis"
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
              setIndex(0);
            }}
            onKeyDown={(event) => {
              if (event.key === "ArrowDown") setIndex((i) => Math.min(i + 1, matches.length - 1));
              if (event.key === "ArrowUp") setIndex((i) => Math.max(i - 1, 0));
              if (event.key === "Enter") choose(matches[index]);
            }}
          />
        </div>
        <ul>
          {matches.map((command, i) => (
            <li
              key={command.id}
              className={i === index ? "selected" : ""}
              onMouseEnter={() => setIndex(i)}
              onClick={() => choose(command)}
            >
              <span>{command.label}</span>
              {command.hint && <span className="muted">{command.hint}</span>}
            </li>
          ))}
          {matches.length === 0 && <li className="muted">No matches</li>}
        </ul>
      </div>
    </div>
  );
}
