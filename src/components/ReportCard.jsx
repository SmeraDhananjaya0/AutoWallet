import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

export default function ReportCard({ topic, provider, report, sources = [] }) {
  return (
    <div className="mt-2 rounded-lg border border-accent/40 bg-accent/5 p-3">
      <div className="mb-1 flex items-center gap-2">
        <span className="rounded bg-accent/20 px-2 py-0.5 text-xs font-medium uppercase tracking-wide text-accent">
          Research report
        </span>
        <span className="text-xs text-muted">
          {sources.length} sources{provider === 'mock' ? ' · mock' : ''}
        </span>
      </div>
      <p className="font-mono text-xs text-muted">"{topic}"</p>
      {report && (
        <details className="mt-2">
          <summary className="cursor-pointer text-xs text-accent">Show full report</summary>
          <div className="agent-markdown mt-2 text-xs leading-relaxed text-text/90">
            <Markdown remarkPlugins={[remarkGfm]}>{report}</Markdown>
          </div>
        </details>
      )}
      {sources.length > 0 && (
        <ul className="mt-2 space-y-1">
          {sources.map((s) => (
            <li key={s.url} className="text-xs">
              <a href={s.url} target="_blank" rel="noreferrer" className="text-accent hover:underline">
                {s.title}
              </a>
              {s.age && <span className="ml-2 text-muted">{s.age}</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
