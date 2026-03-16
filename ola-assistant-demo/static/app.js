const form = document.getElementById("chat-form");
const input = document.getElementById("message-input");
const chatLog = document.getElementById("chat-log");
const sendButton = document.getElementById("send-button");
const statusText = document.getElementById("status-text");
const conversationList = document.getElementById("conversation-list");
const newChatButton = document.getElementById("new-chat-button");

let currentConversationId = null;
let conversations = [];

function addMessage(role, text) {
  const article = document.createElement("article");
  article.className = `message ${role}`;

  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = text;

  article.appendChild(bubble);
  chatLog.appendChild(article);
  chatLog.scrollTop = chatLog.scrollHeight;
  return bubble;
}

function clearMessages() {
  chatLog.innerHTML = "";
}

function renderWelcome() {
  clearMessages();
  addMessage("assistant", "你好，我是 OLA小助手。现在可以开始新的一轮对话了。");
}

function setBusy(isBusy) {
  sendButton.disabled = isBusy;
  input.disabled = isBusy;
  statusText.textContent = isBusy ? "正在请求本地 Codex..." : "本地模式已就绪";
}

function formatTime(timestamp) {
  if (!timestamp) {
    return "";
  }

  return new Date(timestamp * 1000).toLocaleString("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function renderConversationList() {
  conversationList.innerHTML = "";

  if (!conversations.length) {
    const empty = document.createElement("div");
    empty.className = "conversation-empty";
    empty.textContent = "还没有历史会话";
    conversationList.appendChild(empty);
    return;
  }

  conversations.forEach((conversation) => {
    const item = document.createElement("div");
    item.className = "conversation-item";
    if (conversation.id === currentConversationId) {
      item.classList.add("active");
    }

    const mainButton = document.createElement("button");
    mainButton.type = "button";
    mainButton.className = "conversation-main";

    const title = document.createElement("div");
    title.className = "conversation-item-title";
    title.textContent = conversation.title || "未命名对话";

    const preview = document.createElement("div");
    preview.className = "conversation-item-preview";
    preview.textContent = conversation.preview || "点击查看完整记录";

    const meta = document.createElement("div");
    meta.className = "conversation-item-meta";
    meta.textContent = formatTime(conversation.updatedAt);

    mainButton.appendChild(title);
    mainButton.appendChild(preview);
    mainButton.appendChild(meta);
    mainButton.addEventListener("click", () => loadConversation(conversation.id));

    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "conversation-delete";
    deleteButton.textContent = "删除";
    deleteButton.addEventListener("click", async (event) => {
      event.stopPropagation();
      try {
        await deleteConversation(conversation.id);
      } catch (error) {
        statusText.textContent = `删除失败：${error.message}`;
      }
    });

    item.appendChild(mainButton);
    item.appendChild(deleteButton);
    conversationList.appendChild(item);
  });
}

async function refreshConversationList(selectConversationId) {
  const response = await fetch("/api/conversations");
  const data = await response.json();
  conversations = data.data || [];

  if (selectConversationId) {
    currentConversationId = selectConversationId;
  } else if (
    currentConversationId &&
    !conversations.some((conversation) => conversation.id === currentConversationId)
  ) {
    currentConversationId = null;
  }

  renderConversationList();
}

function renderConversationMessages(messages) {
  clearMessages();

  if (!messages.length) {
    renderWelcome();
    return;
  }

  messages.forEach((message) => addMessage(message.role, message.text));
}

async function loadConversation(conversationId) {
  const response = await fetch(`/api/conversations/${conversationId}`);
  const data = await response.json();
  currentConversationId = data.conversation.id;
  renderConversationMessages(data.conversation.messages || []);
  renderConversationList();
}

async function createConversation() {
  const response = await fetch("/api/conversations", { method: "POST" });
  const data = await response.json();
  currentConversationId = data.conversation.id;
  await refreshConversationList(currentConversationId);
  renderWelcome();
}

async function deleteConversation(conversationId) {
  const response = await fetch(`/api/conversations/${conversationId}`, {
    method: "DELETE",
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || "删除失败");
  }

  const wasCurrent = currentConversationId === conversationId;
  await refreshConversationList();

  if (wasCurrent) {
    if (conversations.length) {
      await loadConversation(conversations[0].id);
    } else {
      currentConversationId = null;
      renderWelcome();
      renderConversationList();
    }
  }
}

async function sendMessage(message) {
  addMessage("user", message);
  const pendingBubble = addMessage("assistant", "正在思考，请稍候...");
  setBusy(true);

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ message, conversationId: currentConversationId }),
    });

    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || "请求失败");
    }

    currentConversationId = data.conversation.id;
    pendingBubble.textContent = data.reply;
    await refreshConversationList(currentConversationId);
  } catch (error) {
    pendingBubble.textContent = `请求失败：${error.message}`;
  } finally {
    setBusy(false);
    input.focus();
    chatLog.scrollTop = chatLog.scrollHeight;
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = input.value.trim();
  if (!message) {
    return;
  }

  input.value = "";
  await sendMessage(message);
});

newChatButton.addEventListener("click", async () => {
  await createConversation();
  input.focus();
});

input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    form.requestSubmit();
  }
});

async function bootstrap() {
  await refreshConversationList();
  if (conversations.length) {
    await loadConversation(conversations[0].id);
    return;
  }
  renderWelcome();
}

bootstrap().catch((error) => {
  statusText.textContent = `初始化失败：${error.message}`;
});
