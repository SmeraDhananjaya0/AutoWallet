import Markdown from 'react-markdown';
import ToolCallCard from './ToolCallCard';
import SearchResultCard from './SearchResultCard';
import TopUpCard from './TopUpCard';

export default function MessageBubble({ message }) {
  if (message.role === 'user') {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] rounded-2xl rounded-br-md bg-accent px-4 py-2.5 text-sm text-white">
          {message.content}
        </div>
      </div>
    );
  }

  const hasCards = message.events?.length || message.tool_calls?.length || message.search_results?.length;

  return (
    <div className="flex justify-start">
      <div
        className={`max-w-[85%] rounded-2xl rounded-bl-md border bg-panel px-4 py-2.5 text-sm ${
          message.error ? 'border-spend/50' : 'border-border'
        }`}
      >
        {message.events?.map((ev, i) => (
          <TopUpCard key={`event-${i}`} {...ev} />
        ))}
        {message.tool_calls?.map((tc, i) => (
          <ToolCallCard key={`tool-${i}`} {...tc} />
        ))}
        {message.search_results?.map((sr, i) => (
          <SearchResultCard key={`search-${i}`} {...sr} />
        ))}
        {message.text && (
          <div className={`agent-markdown leading-relaxed text-text/95 ${hasCards ? 'mt-3' : ''}`}>
            <Markdown components={{ a: (props) => <a {...props} target="_blank" rel="noreferrer" /> }}>
              {message.text}
            </Markdown>
          </div>
        )}
      </div>
    </div>
  );
}
