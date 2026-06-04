import { useEffect, useRef } from 'react';
import MessageBubble from './MessageBubble';

const STATUS_STYLES = {
  idle: { label: 'Idle', dot: 'bg-muted', text: 'text-muted' },
  thinking: { label: 'Thinking', dot: 'bg-accent animate-pulse', text: 'text-accent' },
  spending: { label: 'Spending', dot: 'bg-spend animate-pulse', text: 'text-spend' },
};

export default function ChatPanel({
  messages,
  agentStatus,
  inputValue,
  onInputChange,
  onSend,
  isSending,
}) {
  const threadRef = useRef(null);
  const status = STATUS_STYLES[agentStatus] || STATUS_STYLES.idle;

  useEffect(() => {
    if (threadRef.current) {
      threadRef.current.scrollTop = threadRef.current.scrollHeight;
    }
  }, [messages, agentStatus]);

  const handleSubmit = (e) => {
    e.preventDefault();
    onSend();
  };

  return (
    <section className="flex h-full min-h-0 w-[60%] flex-col border-r border-border bg-panel">
      <header className="flex shrink-0 items-center justify-between border-b border-border px-6 py-4">
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-accent/20">
            <svg className="h-5 w-5 text-accent" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M3 10h18M7 15h1m4 0h1m-7 4h12a3 3 0 003-3V8a3 3 0 00-3-3H6a3 3 0 00-3 3v8a3 3 0 003 3z"
              />
            </svg>
          </div>
          <h1 className="text-lg font-semibold tracking-tight text-text">AutoWallet</h1>
        </div>
        <div
          className={`flex items-center gap-2 rounded-full border border-border bg-bg px-3 py-1.5 text-xs font-medium ${status.text}`}
        >
          <span className={`h-2 w-2 rounded-full ${status.dot}`} />
          {status.label}
        </div>
      </header>

      <div ref={threadRef} className="flex-1 space-y-4 overflow-y-auto px-6 py-5">
        {messages.map((msg) => (
          <MessageBubble key={msg.id} message={msg} />
        ))}
        {agentStatus === 'thinking' && (
          <div className="flex justify-start">
            <div className="rounded-2xl rounded-bl-md border border-border bg-panel px-4 py-3">
              <div className="flex gap-1">
                <span className="h-2 w-2 animate-bounce rounded-full bg-muted [animation-delay:0ms]" />
                <span className="h-2 w-2 animate-bounce rounded-full bg-muted [animation-delay:150ms]" />
                <span className="h-2 w-2 animate-bounce rounded-full bg-muted [animation-delay:300ms]" />
              </div>
            </div>
          </div>
        )}
      </div>

      <form
        onSubmit={handleSubmit}
        className="shrink-0 border-t border-border bg-panel px-6 py-4"
      >
        <div className="flex gap-3">
          <input
            type="text"
            value={inputValue}
            onChange={(e) => onInputChange(e.target.value)}
            placeholder="Ask the agent to complete a task…"
            disabled={isSending}
            className="flex-1 rounded-lg border border-border bg-bg px-4 py-2.5 text-sm text-text placeholder:text-muted focus:border-accent focus:outline-none focus:ring-1 focus:ring-accent disabled:opacity-50"
          />
          <button
            type="submit"
            disabled={isSending || !inputValue.trim()}
            className="rounded-lg bg-accent px-5 py-2.5 text-sm font-medium text-white transition hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Send
          </button>
        </div>
      </form>
    </section>
  );
}
