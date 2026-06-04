import { useCallback, useEffect, useState } from 'react';
import ChatPanel from './components/ChatPanel';
import WalletPanel from './components/WalletPanel';
import {
  INITIAL_MESSAGES,
  getBalance,
  getTransactions,
  sendChatMessage,
  topUpWallet,
} from './api';

let messageId = 100;

function nextId() {
  messageId += 1;
  return `msg_${messageId}`;
}

export default function App() {
  const [messages, setMessages] = useState(INITIAL_MESSAGES);
  const [balanceCents, setBalanceCents] = useState(1000);
  const [transactions, setTransactions] = useState([]);
  const [agentStatus, setAgentStatus] = useState('idle');
  const [inputValue, setInputValue] = useState('');
  const [isSending, setIsSending] = useState(false);
  const [isToppingUp, setIsToppingUp] = useState(false);

  const sortTransactions = (txns) =>
    [...txns].sort((a, b) => new Date(b.timestamp) - new Date(a.timestamp));

  const applyWalletFromResponse = useCallback((data) => {
    if (typeof data?.balance_cents === 'number') {
      setBalanceCents(data.balance_cents);
    }
    if (Array.isArray(data?.transactions)) {
      setTransactions(sortTransactions(data.transactions));
    }
  }, []);

  const refreshWallet = useCallback(async () => {
    const [balance, txns] = await Promise.all([getBalance(), getTransactions()]);
    setBalanceCents(balance.balance_cents);
    setTransactions(sortTransactions(txns));
  }, []);

  useEffect(() => {
    refreshWallet();
  }, [refreshWallet]);

  const handleSend = async () => {
    const text = inputValue.trim();
    if (!text || isSending) return;

    const userMsg = { id: nextId(), role: 'user', content: text };
    setMessages((prev) => [...prev, userMsg]);
    setInputValue('');
    setIsSending(true);
    setAgentStatus('thinking');

    try {
      const data = await sendChatMessage(text);

      if (data.tool_calls?.length) {
        setAgentStatus('spending');
        await new Promise((r) => setTimeout(r, 600));
      }

      const agentMsg = {
        id: nextId(),
        role: 'agent',
        text: data.response,
        tool_calls: data.tool_calls || [],
        search_results: data.search_results || [],
      };
      setMessages((prev) => [...prev, agentMsg]);
      applyWalletFromResponse(data);
    } catch (err) {
      setMessages((prev) => [
        ...prev,
        {
          id: nextId(),
          role: 'agent',
          text: `Something went wrong: ${err.message}`,
        },
      ]);
    } finally {
      setIsSending(false);
      setAgentStatus('idle');
    }
  };

  const handleTopUp = async () => {
    if (isToppingUp) return;
    setIsToppingUp(true);
    try {
      const data = await topUpWallet();
      applyWalletFromResponse(data);
    } finally {
      setIsToppingUp(false);
    }
  };

  return (
    <div className="flex h-screen min-w-[1280px] overflow-hidden bg-bg">
      <ChatPanel
        messages={messages}
        agentStatus={agentStatus}
        inputValue={inputValue}
        onInputChange={setInputValue}
        onSend={handleSend}
        isSending={isSending}
      />
      <WalletPanel
        balanceCents={balanceCents}
        transactions={transactions}
        onTopUp={handleTopUp}
        isToppingUp={isToppingUp}
      />
    </div>
  );
}
