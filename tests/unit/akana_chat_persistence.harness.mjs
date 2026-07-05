import assert from "node:assert/strict";

/** Mirrors conversationIdForMemory resolution in akana-chat.js */
function conversationIdForMemory(chatStore, activeByProfile, profile, conversationIdVar) {
  const tid = activeByProfile[profile];
  const thread = tid && chatStore.threads[tid] ? chatStore.threads[tid] : null;
  if (thread?.conversationId) return thread.conversationId;
  return conversationIdVar || "";
}

function isConversationDisplayed(convId, thread, logChildCount) {
  if (!convId) return false;
  return Boolean(thread?.conversationId === convId && logChildCount > 0);
}

const profile = "cursor";
const threadA = {
  id: "t-a",
  profile,
  conversationId: "01THREADAAAAAAAAAAAAAAAA",
  messages: [],
};
const store = {
  activeByProfile: { [profile]: "t-a" },
  threads: { "t-a": threadA },
};

assert.equal(
  conversationIdForMemory(store, store.activeByProfile, profile, "01SESSIONBBBBBBBBBBBBBBBB"),
  "01THREADAAAAAAAAAAAAAAAA",
);

assert.equal(isConversationDisplayed("01THREADAAAAAAAAAAAAAAAA", threadA, 1), true);
assert.equal(isConversationDisplayed("01THREADAAAAAAAAAAAAAAAA", threadA, 0), false);

console.log("akana_chat_persistence.harness: ok");
