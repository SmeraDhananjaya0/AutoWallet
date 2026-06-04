import ToolCallCard from './ToolCallCard';
import SearchResultCard from './SearchResultCard';

export default function MessageBubble({ message }) {
  const isUser = message.role === 'user';

  if (isUser) {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] rounded-2xl rounded-br-md bg-accent px-4 py-2.5 text-sm text-white">
          {message.content}
        </div>
      </div>
    );
  }

  return (
    <div className="flex justify-start">
      <div className="max-w-[85%] rounded-2xl rounded-bl-md border border-border bg-panel px-4 py-2.5 text-sm">
        {message.tool_calls?.map((tc, i) => (
          <ToolCallCard key={`tool-${i}`} {...tc} />
        ))}
        {message.search_results?.map((sr, i) => (
          <SearchResultCard key={`search-${i}`} {...sr} />
        ))}
        {(message.text || message.content) && (
          <p className={`text-text/95 leading-relaxed ${message.tool_calls?.length ? 'mt-3' : ''}`}>
            {message.text || message.content}
          </p>
        )}
      </div>
    </div>
  );
}
