/**
 * `/` — the chat view.
 *
 * Same screen as `/chat` (plan step M6.4 names that path); the implementation
 * lives in `components/ChatView.tsx` so the two cannot drift apart.
 *
 * M2.5's page component used to live here inline and awaited the whole reply
 * body before painting. That is exactly what M6 replaces — see the header of
 * `components/ChatView.tsx`.
 */

import ChatView from "@/components/ChatView";

export default function HomeRoute() {
  return <ChatView />;
}
