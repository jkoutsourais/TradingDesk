export interface Point {
  text: string;
  claim_ids: string[];
  fact_refs: string[];
}

/** Debate or rating points with their citations: claims link to their trace. */
export function Points({ points }: { points: Point[] }) {
  return (
    <ul className="lines">
      {points.map((point, i) => (
        <li key={i}>
          {point.text}
          <div className="muted" style={{ fontSize: 12 }}>
            {point.claim_ids.map((id) => (
              <a key={id} href={`#/trace/${id}`} className="label accent" style={{ marginRight: 4 }}>
                claim
              </a>
            ))}
            {point.fact_refs.map((ref) => (
              <span key={ref} className="mono" style={{ marginRight: 8 }}>
                {ref}
              </span>
            ))}
          </div>
        </li>
      ))}
    </ul>
  );
}
