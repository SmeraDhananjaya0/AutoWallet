import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

const PROVIDER_LABELS = { claude: 'via Claude web search', brave: 'via Brave Search', mock: 'mock results' };

export default function SearchResultCard({ query, provider, results = [], summary }) {
  return (
    <div className="mt-2 rounded-lg border border-border bg-bg/60 p-3">
      <div className="mb-1 flex items-center gap-2">
        <span className="rounded bg-accent/20 px-2 py-0.5 text-xs font-medium uppercase tracking-wide text-accent">
          Search
        </span>
        {PROVIDER_LABELS[provider] && <span className="text-xs text-muted">{PROVIDER_LABELS[provider]}</span>}
      </div>
      <p className="font-mono text-xs text-muted">"{query}"</p>
      {summary && (
        <div className="agent-markdown mt-2 text-xs leading-relaxed text-text/80">
          <Markdown remarkPlugins={[remarkGfm]}>{summary}</Markdown>
        </div>
      )}
      <ul className="mt-2 space-y-2">
        {results.map((r) => (
          <li key={r.url}>
            <a href={r.url} target="_blank" rel="noreferrer" className="text-sm text-accent hover:underline">
              {r.title}
            </a>
            {r.age && <span className="ml-2 text-xs text-muted">{r.age}</span>}
            {r.snippet && <p className="text-xs leading-relaxed text-text/80">{r.snippet}</p>}
          </li>
        ))}
      </ul>
    </div>
  );
}
