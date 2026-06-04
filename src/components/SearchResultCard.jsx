export default function SearchResultCard({ query, snippet }) {
  return (
    <div className="mt-2 rounded-lg border border-border bg-bg/60 p-3">
      <div className="mb-1 flex items-center gap-2">
        <span className="rounded bg-accent/20 px-2 py-0.5 text-xs font-medium uppercase tracking-wide text-accent">
          Search
        </span>
      </div>
      <p className="font-mono text-xs text-muted">"{query}"</p>
      <p className="mt-2 text-sm leading-relaxed text-text/90">{snippet}</p>
    </div>
  );
}
