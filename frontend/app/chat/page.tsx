/**
 * `/chat` — the chat view (plan step M6.4 names this path).
 *
 * The implementation lives in `components/ChatView.tsx` so `/` and `/chat` are
 * the same screen rather than two that drift apart. M2.5 shipped the chat at
 * `/`, and existing links and muscle memory point there.
 */

import ChatView from "@/components/ChatView";

export const metadata = { title: "Chat — memory-system" };

export default function ChatRoute() {
  return <ChatView />;
}
