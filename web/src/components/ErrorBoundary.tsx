import { Component, type ErrorInfo, type ReactNode } from "react";

const RELOAD_KEY = "desk-chunk-reload";
// A page opened before a publish asks for code files the new publish no longer has.
const STALE_CHUNK = /dynamically imported module|Failed to fetch dynamically|Importing a module script failed|error loading dynamically/i;

interface State {
  error: Error | null;
}

/** Shows a view's error instead of a blank page; reloads once when the build changed. */
export class ErrorBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    if (STALE_CHUNK.test(error.message)) {
      let reloaded = false;
      try {
        reloaded = sessionStorage.getItem(RELOAD_KEY) === "1";
        sessionStorage.setItem(RELOAD_KEY, "1");
      } catch {
        reloaded = false;
      }
      if (!reloaded) {
        window.location.reload();
        return;
      }
    }
    console.error("view failed", error, info.componentStack);
  }

  componentDidMount(): void {
    try {
      sessionStorage.removeItem(RELOAD_KEY);
    } catch {
      // Storage can be unavailable (private windows); the reload guard is best effort.
    }
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="content">
        <div className="box">
          <div className="box-header">This view failed to load</div>
          <div className="box-body">
            <div className="mono neg">{this.state.error.message}</div>
            <button className="btn" style={{ marginTop: 8 }} onClick={() => window.location.reload()}>
              Reload
            </button>
          </div>
        </div>
      </div>
    );
  }
}
