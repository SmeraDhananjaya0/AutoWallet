import { useCallback, useEffect, useRef, useState } from 'react';
import ChatPanel from './components/ChatPanel';
import WalletPanel from './components/WalletPanel';
import {
  WELCOME_MESSAGE,
  getBalance,
  getTransactions,
  getWalletStatus,
  sendChatMessage,
  setDemoBalance,
  topUpCard,
  topUpRobinhood,
} from './api';

export default function App() {
  const nextId = useRef(0);
  const newId = () => `msg_${(nextId.current += 1)}`;

  const [messages, setMessages] = useState([WELCOME_MESSAGE]);
  const [balanceUsd, setBalanceUsd] = useState(0);
  const [transactions, setTransactions] = useState([]);
  const [walletStatus, setWalletStatus] = useState(null);
  const [agentStatus, setAgentStatus] = useState('idle');
  const [inputValue, setInputValue] = useState('');
  const [isSending, setIsSending] = useState(false);
  const [busyAction, setBusyAction] = useState(null);
  const [walletError, setWalletError] = useState(null);
  const [sessionId, setSessionId] = useState(null);

  const applyWallet = useCallback((data) => {
    if (typeof data?.balance_micros === 'number') setBalanceUsd(data.balance_micros / 1_000_000);
    if (Array.isArray(data?.transactions)) setTransactions(data.transactions);
  }, []);

  const refreshStatus = useCallback(() => {
    getWalletStatus().then(setWalletStatus).catch(() => {});
  }, []);

  const refreshWallet = useCallback(async () => {
    try {
      const [balance, txns] = await Promise.all([getBalance(), getTransactions()]);
      applyWallet({ ...balance, transactions: txns });
      setWalletError(null);
    } catch (err) {
      setWalletError(`Backend unreachable: ${err.message}`);
    }
    refreshStatus();
  }, [applyWallet, refreshStatus]);

  useEffect(() => {
    refreshWallet();
  }, [refreshWallet]);

  const handleSend = async () => {
    const text = inputValue.trim();
    if (!text || isSending) return;

    setMessages((prev) => [...prev, { id: newId(), role: 'user', content: text }]);
    setInputValue('');
    setIsSending(true);
    setAgentStatus('thinking');

    try {
      const data = await sendChatMessage(text, sessionId);
      if (data.session_id) setSessionId(data.session_id);

      if (data.tool_calls?.length) {
        setAgentStatus('spending');
        await new Promise((r) => setTimeout(r, 500));
      }

      setMessages((prev) => [
        ...prev,
        {
          id: newId(),
          role: 'agent',
          text: data.response,
          tool_calls: data.tool_calls || [],
          search_results: data.search_results || [],
          events: data.events || [],
        },
      ]);
      applyWallet(data);
      setWalletError(null);
      refreshStatus();
    } catch (err) {
      setMessages((prev) => [
        ...prev,
        { id: newId(), role: 'agent', error: true, text: `Something went wrong: ${err.message}` },
      ]);
    } finally {
      setIsSending(false);
      setAgentStatus('idle');
    }
  };

  const runWalletAction = async (name, action) => {
    if (busyAction) return;
    setBusyAction(name);
    setWalletError(null);
    try {
      applyWallet(await action());
    } catch (err) {
      setWalletError(err.message);
      if (err.data) applyWallet(err.data);
    } finally {
      setBusyAction(null);
      refreshStatus();
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
        balanceUsd={balanceUsd}
        transactions={transactions}
        walletStatus={walletStatus}
        busyAction={busyAction}
        error={walletError}
        onCardTopUp={() => runWalletAction('card', topUpCard)}
        onRobinhoodTopUp={() => runWalletAction('robinhood', topUpRobinhood)}
        onDrain={() => runWalletAction('drain', () => setDemoBalance(0.002))}
      />
    </div>
  );
}
